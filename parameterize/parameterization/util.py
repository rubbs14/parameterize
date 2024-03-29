# (c) 2015-2018 Acellera Ltd http://www.acellera.com
# All Rights Reserved
# Distributed under HTMD Software License Agreement
# No redistribution in whole or part
#
import numpy as np
import logging
import re
import os
import subprocess
from tempfile import TemporaryDirectory, TemporaryFile

logger = logging.getLogger(__name__)


def guessElements(mol, method):
    """
    Guess element from an atom name
    """

    elements = {}
    elements["CGenFF_2b6"] = ["H", "C", "N", "O", "F", "S", "P", "Cl", "Br", "I"]
    elements["GAFF"] = ["H", "C", "N", "O", "F", "S", "P", "Cl", "Br", "I"]
    elements["GAFF2"] = ["H", "C", "N", "O", "F", "S", "P", "Cl", "Br", "I"]
    elements["ANI-1x"] = ["H", "C", "N", "O"]
    elements["ANI-2x"] = ["H", "C", "N", "O", "F", "Cl", "S"]

    if method not in elements.keys():
        raise ValueError(
            'Invalid "method": {}. Valid methods: {}'
            "".format(method, ",".join(elements.keys()))
        )

    mol = mol.copy()

    for i, name in enumerate(mol.name):

        candidates = [
            element
            for element in elements[method]
            if name.capitalize().startswith(element)
        ]

        if len(candidates) == 1:
            mol.element[i] = candidates[0]
            continue

        if candidates == ["C", "Cl"]:

            if len(mol.bonds) == 0:
                raise RuntimeError("No chemical bonds found in the molecule")

            # Create a molecular graph
            import networkx as nx

            graph = nx.Graph()
            graph.add_edges_from(mol.bonds)

            if len(graph[i]) in (2, 3, 4):
                mol.element[i] = "C"
                continue

            if len(graph[i]) == 1:
                mol.element[i] = "Cl"
                continue

        raise ValueError(
            "Cannot guess element from atom name: {}. "
            "It does not match any of the expected elements ({}) for {}."
            "".format(name, ", ".join(elements[method]), method)
        )

    return mol


def centreOfMass(mol):
    return np.dot(mol.masses, mol.coords[:, :, mol.frame]) / np.sum(mol.masses)


def getDipole(mol):
    """Calculate the dipole moment (in Debyes) of the molecule"""
    from scipy import constants as const

    if mol.masses.sum() == 0:
        logger.warning("No masses found in Molecule. Cannot calculate dipole.")
        return np.zeros(4)
    else:
        coords = mol.coords[:, :, mol.frame] - centreOfMass(mol)

    dipole = np.zeros(4)
    dipole[:3] = np.dot(mol.charge, coords)
    dipole[3] = np.linalg.norm(dipole[:3])  # Total dipole moment
    dipole *= (
        1e11 * const.elementary_charge * const.speed_of_light
    )  # e * Ang --> Debye (https://en.wikipedia.org/wiki/Debye)

    return dipole


def _qm_method_name(qm):
    basis = qm.basis
    basis = re.sub("\+", "plus", basis)  # Replace '+' with 'plus'
    basis = re.sub("\*", "star", basis)  # Replace '*' with 'star'
    name = qm.theory + "-" + basis + "-" + qm.solvent
    return name


def getFixedChargeAtomIndices(mol, fix_charge):
    fixed_atom_indices = []
    for fixed_atom_name in fix_charge:

        if fixed_atom_name not in mol.name:
            raise ValueError(
                "Atom {} is not found. Check --fix-charge arguments".format(
                    fixed_atom_name
                )
            )

        for aton_index in range(mol.numAtoms):
            if mol.name[aton_index] == fixed_atom_name:
                fixed_atom_indices.append(aton_index)
                logger.info(
                    "Charge of atom {} is fixed to {}".format(
                        fixed_atom_name, mol.charge[aton_index]
                    )
                )
    return fixed_atom_indices


def minimize(mol, qm, outdir, min_type="qm", mm_minimizer=None):

    assert mol.numFrames == 1
    mol = mol.copy()

    if min_type == "qm":
        mindir = os.path.join(outdir, "minimize", _qm_method_name(qm))
        os.makedirs(mindir, exist_ok=True)

        qm.molecule = mol
        qm.esp_points = None
        qm.optimize = True
        qm.restrained_dihedrals = None
        qm.directory = mindir
        results = qm.run()
        if results[0].errored:
            raise RuntimeError("\nQM minimization failed! Check logs at %s\n" % mindir)

        # Replace coordinates with the minimized set
        mol.coords = np.atleast_3d(np.array(results[0].coords, dtype=np.float32))
    elif min_type == "mm":
        mol.coords[:, :, 0] = mm_minimizer.minimize(mol.coords)
    elif min_type == "None":
        pass
    else:
        raise RuntimeError(
            "Invalid minimization mode {}. Check parameterize help.".format(min_type)
        )

    return mol


def scanDihedrals(mol, ref, dihedrals, outdir, scan_type="qm", mm_minimizer=None):
    """
    Dihedrals passed as 4 atom indices
    """
    num_rotamers = 36  # Number of rotamers for each dihedral to compute

    logger.info("Number of rotamers per dihedral angles: {}".format(num_rotamers))

    # Create molecules with rotamers
    # TODO factor out dihedral generation
    logger.info("Generate rotamers for:")
    molecules = []
    for idihed, dihedral in enumerate(dihedrals):
        logger.info(
            "  {:2d}: {}".format(idihed + 1, "-".join(mol.name[list(dihedral)]))
        )

        # Create a copy of molecule with "nrotamers" frames
        new_mol = mol.copy()
        new_mol.coords = np.tile(new_mol.coords, (1, 1, num_rotamers))
        new_mol.box = np.zeros((3, new_mol.numFrames), dtype=np.float32)

        # Set rotamer coordinates
        angles = np.linspace(-np.pi, np.pi, num=num_rotamers, endpoint=False)
        for frame, angle in enumerate(angles):
            new_mol.frame = frame
            new_mol.setDihedral(dihedral, angle, bonds=new_mol.bonds)

        molecules.append(new_mol)

    # Minimize with MM if requested
    if scan_type == "mm":
        logger.info("Minimize rotamers with MM for:")
        for idihed, (dihedral, molecule) in enumerate(zip(dihedrals, molecules)):
            logger.info(
                "  {:2d}: {}".format(idihed + 1, "-".join(mol.name[list(dihedral)]))
            )
            for iframe in range(molecule.numFrames):
                molecule.coords[:, :, iframe] = mm_minimizer.minimize(
                    molecule.coords[:, :, iframe], restrained_dihedrals=[dihedral]
                )

    # Create directories for QM data
    directories = []
    dihedral_directory = (
        "dihedral-opt" if scan_type == "qm" else "dihedral-single-point"
    )
    for dihedral in dihedrals:
        dihedral_name = "-".join(mol.name[dihedral])
        directory = os.path.join(
            outdir, dihedral_directory, dihedral_name, _qm_method_name(ref)
        )
        os.makedirs(directory, exist_ok=True)
        directories.append(directory)

    # Setup and submit QM calculations
    for molecule, dihedral, directory in zip(molecules, dihedrals, directories):
        ref.molecule = molecule
        ref.esp_points = None
        ref.optimize = scan_type == "qm"
        ref.restrained_dihedrals = np.array([dihedral])
        ref.directory = directory
        ref.setup()
        ref.submit()

    # Wait and retrieve QM calculation data
    scan_results = []
    logger.info("Compute rotamer energies for:")
    for idihed, (molecule, dihedral, directory) in enumerate(
        zip(molecules, dihedrals, directories)
    ):
        logger.info(
            "  {:2d}: {}".format(idihed + 1, "-".join(mol.name[list(dihedral)]))
        )
        ref.molecule = molecule
        ref.esp_points = None
        ref.optimize = scan_type == "qm"
        ref.restrained_dihedrals = np.array([dihedral])
        ref.directory = directory
        ref.setup()  # QM object is reused, so it has to be properly set up before retrieving.
        scan_results.append(ref.retrieve())

    return scan_results


def guessBondType(mol):

    """
    Guess bond type with Antechamber

    Parameters
    ----------
    mol: Molecule
        Molecule to guess the bond types

    Return
    ------
    results: Molecule
        Copy of the molecule with the bond type set

    Examples
    --------
    >>> from parameterize.home import home
    >>> from moleculekit.molecule import Molecule

    >>> molFile = os.path.join(home('test-qm'), 'H2O.mol2')
    >>> mol = Molecule(molFile)
    >>> mol.bondtype[:] = "un"

    >>> new_mol = guessBondType(mol)
    >>> assert new_mol is not mol
    >>> new_mol.bondtype
    array(['1', '1'], dtype=object)

    >>> molFile = os.path.join(home('test-param'), 'H2O2.mol2')
    >>> mol = Molecule(molFile)
    >>> mol.bondtype[:] = "un"

    >>> new_mol = guessBondType(mol)
    >>> assert new_mol is not mol
    >>> new_mol.bondtype
    array(['1', '1', '1'], dtype=object)

    >>> molFile = os.path.join(home('test-param'), 'benzamidine.mol2')
    >>> mol = Molecule(molFile)
    >>> mol.bondtype[:] = "un"

    >>> new_mol = guessBondType(mol)
    >>> assert new_mol is not mol
    >>> new_mol.bondtype
    array(['ar', 'ar', '1', 'ar', '1', 'ar', '1', 'ar', '1', 'ar', '1', '1',
           '2', '1', '1', '1', '1', '1'], dtype=object)

    """

    from moleculekit.molecule import Molecule

    if not isinstance(mol, Molecule):
        raise TypeError('"mol" has to be instance of {}'.format(Molecule))
    if mol.numFrames != 1:
        raise ValueError(
            '"mol" can have just one frame, but it has {}'.format(mol.numFrames)
        )

    mol = mol.copy()

    with TemporaryDirectory() as tmpDir:
        old_name = os.path.join(tmpDir, "old.mol2")
        new_name = os.path.join(tmpDir, "new.mol2")

        mol.write(old_name)

        cmd = [
            "antechamber",
            "-fi",
            "mol2",
            "-i",
            old_name,
            "-fo",
            "mol2",
            "-o",
            new_name,
        ]

        with TemporaryFile() as stream:
            if subprocess.call(cmd, cwd=tmpDir, stdout=stream, stderr=stream) != 0:
                raise RuntimeError('"antechamber" failed')
            stream.seek(0)
            for line in stream.readlines():
                logger.debug(line)

        mol.bondtype[:] = Molecule(new_name).bondtype

    return mol


def makeAtomNamesUnique(mol):
    """
    Make atom names unique by appending/incrementing terminal digits.
    Already unique names are preserved.

    Parameters
    ----------
    mol: Molecule
        Molecule to make atom name unique

    Return
    ------
    results: Molecule
        Copy of the molecule with the atom names set

    Examples
    --------
    >>> from parameterize.home import home
    >>> from moleculekit.molecule import Molecule
    >>> molFile = os.path.join(home('test-param'), 'H2O2.mol2')
    >>> mol = Molecule(molFile)

    >>> mol.name[:] = ['A', 'A', 'A', 'A']
    >>> new_mol = makeAtomNamesUnique(mol)
    >>> assert new_mol is not mol
    >>> new_mol.name
    array(['A', 'A0', 'A1', 'A2'], dtype=object)

    >>> mol.name[:] = ['A', 'A', 'A', 'A0']
    >>> new_mol = makeAtomNamesUnique(mol)
    >>> assert new_mol is not mol
    >>> new_mol.name
    array(['A', 'A1', 'A2', 'A0'], dtype=object)

    >>> mol.name[:] = ['A', 'B', 'A', 'B']
    >>> new_mol = makeAtomNamesUnique(mol)
    >>> assert new_mol is not mol
    >>> new_mol.name
    array(['A', 'B', 'A0', 'B0'], dtype=object)

    >>> mol.name[:] = ['A', 'B', 'C', 'D']
    >>> new_mol = makeAtomNamesUnique(mol)
    >>> assert new_mol is not mol
    >>> new_mol.name
    array(['A', 'B', 'C', 'D'], dtype=object)

    >>> mol.name[:] = ['1A', '1A', 'A1B1', 'A1B1']
    >>> new_mol = makeAtomNamesUnique(mol)
    >>> assert new_mol is not mol
    >>> new_mol.name
    array(['1A', '1A0', 'A1B1', 'A1B2'], dtype=object)
    """

    from moleculekit.molecule import Molecule

    if not isinstance(mol, Molecule):
        raise TypeError('"mol" has to be an instance of {}'.format(Molecule))

    mol = mol.copy()

    for i, name in enumerate(mol.name):
        while np.sum(name == mol.name) > 1:  # Check for identical names
            j = np.flatnonzero(name == mol.name)[
                1
            ]  # Get the second identical name index
            prefix, sufix = re.match("(.*?\D*)(\d*)$", mol.name[j]).groups()
            sufix = 0 if sufix == "" else int(sufix)
            while prefix + str(sufix) in mol.name:  # Search for a unique name
                sufix += 1
            mol.name[j] = prefix + str(sufix)

    return mol


def detectChiralCenters(mol, atom_types=None):
    """
    Detect chiral centers

    Parameters
    ----------
    mol: Molecule
        Molecule to detect chiral centers

    Return
    ------
    results: List of tuples
        List of chircal centers, where the chiral centers are tuples made of an atom index and a label ('S', 'R', '?').

    Examples
    --------
    >>> from parameterize.home import home
    >>> from moleculekit.molecule import Molecule

    >>> molFile = os.path.join(home('test-param'), 'H2O2.mol2')
    >>> mol = Molecule(molFile)
    >>> detectChiralCenters(mol)
    []

    >>> molFile = os.path.join(home('test-param'), 'fluorchlorcyclopronol.mol2')
    >>> mol = Molecule(molFile)
    >>> detectChiralCenters(mol)
    [(0, 'R'), (2, 'S'), (4, 'R')]

    >>> molFile = os.path.join(home('test-param'), 'fluorchlorcyclopronol.mol2')
    >>> mol = Molecule(molFile)
    >>> detectChiralCenters(mol, atom_types=mol.atomtype)
    [(0, 'R'), (2, 'S'), (4, 'R')]
    """

    from moleculekit.molecule import Molecule
    from moleculekit.rdkitintegration import _convertMoleculeToRDKitMol
    from rdkit.Chem import AssignAtomChiralTagsFromStructure, FindMolChiralCenters

    if not isinstance(mol, Molecule):
        raise TypeError('"mol" has to be instance of {}'.format(Molecule))
    if mol.numFrames != 1:
        raise ValueError(
            '"mol" can have just one frame, but it has {}'.format(mol.numFrames)
        )

    # Set atom types, overwise rdkit refuse to read some MOL2 files
    htmd_mol = mol.copy()
    if atom_types is not None:
        htmd_mol.atomtype = atom_types

    # Detect chiral centers and assign their labels
    rdkit_mol = _convertMoleculeToRDKitMol(htmd_mol)
    AssignAtomChiralTagsFromStructure(rdkit_mol)
    chiral_centers = FindMolChiralCenters(rdkit_mol, includeUnassigned=True)

    return chiral_centers


def filterQMResults(all_results, mol=None):
    """
    Filter QM results

    Parameters
    ----------
    all_results: list of list of QMResult
        QM results
    mol: Molecule
        A molecule corresponding to the QM results

    Return
    ------
    valid_results: List of list of QMResult
        Valid QM results

    Examples
    --------
    >>> from parameterize.qm import QMResult
    >>> results = [QMResult() for _ in range(20)]
    >>> for result in results:
    ...     result.energy = 0
    >>> all_results = [results]

    >>> valid_results  = filterQMResults(all_results)
    >>> len(valid_results)
    1
    >>> len(valid_results[0])
    20

    >>> results[1].errored = True
    >>> results[19].errored = True
    >>> len(filterQMResults(all_results)[0])
    18

    >>> results[10].energy = -5
    >>> results[12].energy = 12
    >>> results[15].energy = 17
    >>> len(filterQMResults(all_results)[0])
    17
    """

    from parameterize.qm import QMResult
    from moleculekit.molecule import Molecule

    if mol:
        if not isinstance(mol, Molecule):
            raise TypeError('"mol" has to be instance of {}'.format(Molecule))
        initial_chiral_centers = detectChiralCenters(mol)
        mol = mol.copy()

    all_valid_results = []
    for results in all_results:

        valid_results = []
        for result in results:

            # Remove failed calculations
            if not isinstance(result, QMResult):
                raise TypeError(
                    '"results" has to a list of list of {} instance'.format(QMResult)
                )
            if result.errored:
                logger.warning("Rotamer is removed due to a failed QM calculations")
                continue

            # Remove results with wrong chiral centers
            if mol:
                mol.coords = result.coords
                chiral_centers = detectChiralCenters(mol)
                if initial_chiral_centers != chiral_centers:
                    logger.warning(
                        "Rotamer is removed due to a change of chiral centers: "
                        "{} --> {}".format(initial_chiral_centers, chiral_centers)
                    )
                    continue

            valid_results.append(result)

        # Remove results with too high energies (>20 kcal/mol above the minimum)
        if len(valid_results) > 0:
            minimum_energy = np.min([result.energy for result in valid_results])

            new_results = []
            for result in valid_results:
                relative_energy = result.energy - minimum_energy
                if relative_energy < 20:  # kcal/mol
                    new_results.append(result)
                else:
                    logger.warning(
                        "Rotamer is removed due to high energy: "
                        "{} kcal/mol above minimum".format(relative_energy)
                    )
            valid_results = new_results

        all_valid_results.append(valid_results)

    return all_valid_results


if __name__ == "__main__":

    import sys
    import doctest

    # Pre-import QMResult to silence import messages in the doctests
    from parameterize.qm import QMResult

    sys.exit(doctest.testmod().failed)
