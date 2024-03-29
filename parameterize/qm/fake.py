# (c) 2015-2018 Acellera Ltd http://www.acellera.com
# All Rights Reserved
# Distributed under HTMD Software License Agreement
# No redistribution in whole or part
#
import os
import pickle
import logging
from subprocess import call
from tempfile import TemporaryDirectory

import numpy as np
from scipy import constants as const
from scipy.spatial.distance import cdist
import nlopt
from simtk import unit
from simtk import openmm
from simtk.openmm import app

from moleculekit.dihedral import dihedralAngle
from parameterize.qm.base import QMBase, QMResult
from ffevaluation.ffevaluate import FFEvaluate
from parameterize.parameterization.util import getDipole

logger = logging.getLogger(__name__)


class FakeQM(QMBase):
    """
    Imitation of QM calculations with MM

    >>> import os
    >>> import numpy as np
    >>> from tempfile import TemporaryDirectory
    >>> from parameterize.home import home
    >>> from moleculekit.dihedral import dihedralAngle
    >>> from parameterize.parameterization.fftype import fftype
    >>> from moleculekit.molecule import Molecule
    >>> from parameterize.qm.fake import FakeQM

    Create a molecule
    >>> molFile = os.path.join(home('test-qm'), 'H2O2-90.mol2')
    >>> mol = Molecule(molFile, guessNE='bonds', guess=('angles', 'dihedrals'))
    >>> parameters, mol = fftype(mol, method='GAFF2')

    Run a single-point energy and ESP calculation
    >>> with TemporaryDirectory() as tmpDir:
    ...     qm = FakeQM()
    ...     qm.molecule = mol
    ...     qm._parameters = parameters
    ...     qm.esp_points = np.array([[1., 1., 1.]])
    ...     qm.directory = tmpDir
    ...     result = qm.run()[0]

    >>> qm # doctest: +ELLIPSIS
    <parameterize.qm.fake.FakeQM object at ...>
    >>> result # doctest: +ELLIPSIS
    <parameterize.qm.base.QMResult object at ...
    >>> result.errored
    False
    >>> result.energy # doctest: +ELLIPSIS
    8.38083...
    >>> result.esp_points
    array([[1., 1., 1.]])
    >>> result.esp_values # doctest: +ELLIPSIS
    array([0.37135...])
    >>> np.rad2deg(dihedralAngle(result.coords[[2, 0, 1, 3], :, 0])) # doctest: +ELLIPSIS
    89.99...

    Run a minimization
    >>> with TemporaryDirectory() as tmpDir:
    ...     qm = FakeQM()
    ...     qm.molecule = mol
    ...     qm._parameters = parameters
    ...     qm.optimize = True
    ...     qm.directory = tmpDir
    ...     result = qm.run()[0]
    >>> result.energy # doctest: +ELLIPSIS
    7.697...
    >>> np.rad2deg(dihedralAngle(result.coords[[2, 0, 1, 3], :, 0])) # doctest: +ELLIPSIS
    102.57...

    Run a constrained minimization
    >>> with TemporaryDirectory() as tmpDir:
    ...     qm = FakeQM()
    ...     qm.molecule = mol
    ...     qm._parameters = parameters
    ...     qm.optimize = True
    ...     qm.restrained_dihedrals = np.array([[2, 0, 1, 3]])
    ...     qm.directory = tmpDir
    ...     result = qm.run()[0]
    >>> result.energy # doctest: +ELLIPSIS
    7.872...
    >>> np.rad2deg(dihedralAngle(result.coords[[2, 0, 1, 3], :, 0])) # doctest: +ELLIPSIS
    89.99...
    """

    # Fake implementations of the abstract methods
    def _command(self):
        pass

    def _writeInput(self, directory, iframe):
        pass

    def _readOutput(self, directory):
        pass

    def setup(self):
        pass

    def submit(self):
        pass

    def __init__(self):
        super().__init__()
        self._parameters = None

    def _completed(self, directory):
        return os.path.exists(os.path.join(directory, "data.pkl"))

    def retrieve(self):

        ff = FFEvaluate(self.molecule, self._parameters)

        results = []
        for iframe in range(self.molecule.numFrames):
            self.molecule.frame = iframe

            directory = os.path.join(self.directory, "%05d" % iframe)
            os.makedirs(directory, exist_ok=True)
            pickleFile = os.path.join(directory, "data.pkl")

            if self._completed(directory):
                with open(pickleFile, "rb") as fd:
                    result = pickle.load(fd)
                logger.info("Loading QM data from %s" % pickleFile)

            else:
                result = QMResult()
                result.errored = False
                result.coords = self.molecule.coords[:, :, iframe : iframe + 1].copy()

                if self.optimize:
                    opt = nlopt.opt(nlopt.LN_COBYLA, result.coords.size)
                    opt.set_min_objective(
                        lambda x, _: ff.calculateEnergies(x.reshape((-1, 3)))["total"]
                    )
                    if self.restrained_dihedrals is not None:
                        for dihedral in self.restrained_dihedrals:
                            indices = dihedral.copy()
                            ref_angle = dihedralAngle(
                                self.molecule.coords[indices, :, iframe]
                            )

                            def constraint(x, _):
                                coords = x.reshape((-1, 3))
                                angle = dihedralAngle(coords[indices])
                                return np.sin(0.5 * (angle - ref_angle))

                            opt.add_equality_constraint(constraint)
                    opt.set_xtol_abs(1e-3)  # Similar to Psi4 default
                    opt.set_maxeval(1000 * opt.get_dimension())
                    opt.set_initial_step(1e-3)
                    result.coords = opt.optimize(result.coords.ravel()).reshape(
                        (-1, 3, 1)
                    )
                    logger.info("Optimization status: %d" % opt.last_optimize_result())

                result.energy = ff.calculateEnergies(result.coords[:, :, 0])["total"]
                result.dipole = getDipole(self.molecule)

                if self.optimize:
                    assert (
                        opt.last_optimum_value() == result.energy
                    )  # A self-consistency test

                # Compute ESP values
                if self.esp_points is not None:
                    assert self.molecule.numFrames == 1
                    result.esp_points = self.esp_points
                    distances = cdist(
                        result.esp_points, result.coords[:, :, 0]
                    )  # Angstrom
                    distances *= (
                        const.physical_constants["Bohr radius"][0] / const.angstrom
                    )  # Angstrom --> Bohr
                    result.esp_values = np.dot(
                        np.reciprocal(distances), self.molecule.charge
                    )  # Hartree/Bohr

                with open(pickleFile, "wb") as fd:
                    pickle.dump(result, fd)

            results.append(result)

        return results


class FakeQM2(FakeQM):
    """
    Imitation of QM calculations with MM

    >>> import os
    >>> import numpy as np
    >>> from tempfile import TemporaryDirectory
    >>> from parameterize.home import home
    >>> from moleculekit.dihedral import dihedralAngle
    >>> from parameterize.parameterization.fftype import fftype
    >>> from moleculekit.molecule import Molecule
    >>> from parameterize.qm.fake import FakeQM2

    Create a molecule
    >>> molFile = os.path.join(home('test-qm'), 'H2O2-90.mol2')
    >>> mol = Molecule(molFile, guessNE='bonds', guess=('angles', 'dihedrals'))
    >>> parameters, mol = fftype(mol, method='GAFF2')

    Run a single-point energy and ESP calculation
    >>> with TemporaryDirectory() as tmpDir:
    ...     qm = FakeQM2()
    ...     qm.molecule = mol
    ...     qm.esp_points = np.array([[1., 1., 1.]])
    ...     qm._parameters = parameters
    ...     qm.directory = tmpDir
    ...     result = qm.run()[0]

    >>> qm # doctest: +ELLIPSIS
    <parameterize.qm.fake.FakeQM2 object at ...>
    >>> result # doctest: +ELLIPSIS
    <parameterize.qm.base.QMResult object at ...
    >>> result.errored
    False
    >>> result.energy # doctest: +ELLIPSIS
    8.380840...
    >>> result.esp_points
    array([[1., 1., 1.]])
    >>> result.esp_values # doctest: +ELLIPSIS
    array([0.371352...])
    >>> np.rad2deg(dihedralAngle(result.coords[[2, 0, 1, 3], :, 0])) # doctest: +ELLIPSIS
    89.99954...

    Run a minimization
    >>> with TemporaryDirectory() as tmpDir:
    ...     qm = FakeQM2()
    ...     qm.molecule = mol
    ...     qm._parameters = parameters
    ...     qm.optimize = True
    ...     qm.directory = tmpDir
    ...     result = qm.run()[0]
    >>> result.energy # doctest: +ELLIPSIS
    7.69312...
    >>> np.rad2deg(dihedralAngle(result.coords[[2, 0, 1, 3], :, 0])) # doctest: +ELLIPSIS
    104.821...

    Run a constrained minimization
    >>> with TemporaryDirectory() as tmpDir:
    ...     qm = FakeQM2()
    ...     qm.molecule = mol
    ...     qm._parameters = parameters
    ...     qm.optimize = True
    ...     qm.restrained_dihedrals = np.array([[2, 0, 1, 3]])
    ...     qm.directory = tmpDir
    ...     result = qm.run()[0]
    >>> result.energy # doctest: +ELLIPSIS
    7.869959...
    >>> np.rad2deg(dihedralAngle(result.coords[[2, 0, 1, 3], :, 0])) # doctest: +ELLIPSIS
    90.07949...
    """

    def _get_prmtop(self):
        from parameterize.parameterization.writers import (
            writeFRCMOD,
            getAtomTypeMapping,
        )

        with TemporaryDirectory() as tmpDir:
            frcFile = os.path.join(tmpDir, "mol.frcmod")
            mapping = getAtomTypeMapping(self._parameters)
            writeFRCMOD(self.molecule, self._parameters, frcFile, typemap=mapping)
            mol2 = self.molecule.copy()
            mol2.atomtype[:] = np.vectorize(mapping.get)(mol2.atomtype)
            molFile = os.path.join(tmpDir, "mol.mol2")
            mol2.write(molFile)

            with open(os.path.join(tmpDir, "tleap.inp"), "w") as file:
                file.writelines(
                    (
                        "loadAmberParams %s\n" % frcFile,
                        "MOL = loadMol2 %s\n" % molFile,
                        "saveAmberParm MOL mol.prmtop mol.inpcrd\n",
                        "quit",
                    )
                )

            with open(os.path.join(tmpDir, "tleap.out"), "w") as out:
                call(("tleap", "-f", "tleap.inp"), cwd=tmpDir, stdout=out)

            prmtop = app.AmberPrmtopFile(os.path.join(tmpDir, "mol.prmtop"))

        return prmtop

    def retrieve(self):

        prmtop = self._get_prmtop()
        system = prmtop.createSystem()
        groups = {force.getForceGroup() for force in system.getForces()}

        if self.optimize:
            if self.restrained_dihedrals is not None:
                restraint = openmm.PeriodicTorsionForce()
                restraint.setForceGroup(max(groups) + 1)

                for dihedral in self.restrained_dihedrals:
                    restraint.addTorsion(
                        *tuple(map(int, dihedral)),
                        periodicity=1,
                        phase=0,
                        k=-1000 * unit.kilocalorie_per_mole
                    )

                system.addForce(restraint)

        simulation = app.Simulation(
            prmtop.topology,
            system,
            openmm.VerletIntegrator(1 * unit.femtosecond),
            openmm.Platform.getPlatformByName("CPU"),
        )

        results = []
        molecule_copy = self.molecule.copy()
        for iframe in range(self.molecule.numFrames):
            self.molecule.frame = iframe
            molecule_copy.frame = iframe

            directory = os.path.join(self.directory, "%05d" % iframe)
            os.makedirs(directory, exist_ok=True)
            pickleFile = os.path.join(directory, "data.pkl")

            if self._completed(directory):
                with open(pickleFile, "rb") as fd:
                    results.append(pickle.load(fd))
                logger.info("Loading QM data from %s" % pickleFile)
                continue

            simulation.context.setPositions(
                self.molecule.coords[:, :, iframe] * unit.angstrom
            )
            if self.optimize:
                if self.restrained_dihedrals is not None:
                    for i, dihedral in enumerate(self.restrained_dihedrals):
                        ref_angle = np.rad2deg(
                            dihedralAngle(self.molecule.coords[dihedral, :, iframe])
                        )
                        parameters = restraint.getTorsionParameters(i)
                        parameters[5] = ref_angle * unit.degree
                        restraint.setTorsionParameters(i, *parameters)
                    restraint.updateParametersInContext(simulation.context)
                simulation.minimizeEnergy(tolerance=0.001 * unit.kilocalorie_per_mole)
            state = simulation.context.getState(
                getEnergy=True, getPositions=True, groups=groups
            )

            result = QMResult()
            result.charge = self.charge
            result.errored = False
            result.energy = state.getPotentialEnergy().value_in_unit(
                unit.kilocalorie_per_mole
            )
            result.coords = (
                state.getPositions(asNumpy=True)
                .value_in_unit(unit.angstrom)
                .reshape((-1, 3, 1))
            )
            result.dipole = getDipole(self.molecule)

            if self.esp_points is not None:
                assert self.molecule.numFrames == 1
                result.esp_points = self.esp_points
                distances = cdist(result.esp_points, result.coords[:, :, 0])  # Angstrom
                distances *= (
                    const.physical_constants["Bohr radius"][0] / const.angstrom
                )  # Angstrom --> Bohr
                result.esp_values = np.dot(
                    np.reciprocal(distances), self.molecule.charge
                )  # Hartree/Bohr

            results.append(result)

            with open(pickleFile, "wb") as fd:
                pickle.dump(result, fd)

            self.molecule.write(
                os.path.join(directory, "mol-init.mol2")
            )  # Write an optimiz
            molecule_copy.coords[:, :, iframe] = result.coords[:, :, 0]
            molecule_copy.write(os.path.join(directory, "mol.mol2"))  # Write an optimiz

        return results


if __name__ == "__main__":
    import sys
    import doctest

    if doctest.testmod().failed:
        sys.exit(1)
