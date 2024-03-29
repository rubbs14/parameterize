# (c) 2015-2017 Acellera Ltd http://www.acellera.com
# All Rights Reserved
# Distributed under HTMD Software License Agreement
# No redistribution in whole or part
#
import os
import time
import pickle
import logging
import abc

import numpy as np
import nlopt

from moleculekit.dihedral import dihedralAngle
from parameterize.qm.base import QMBase, QMResult
from protocolinterface import val


logger = logging.getLogger(__name__)


class Minimizer(abc.ABC):
    def __init__(self):
        pass

    @abc.abstractmethod
    def minimize(self, coords, restrained_dihedrals):
        pass


class OMMMinimizer(Minimizer):
    def __init__(self, mol, prm, platform="CPU", device=0, buildff="AMBER"):
        """ A minimizer based on OpenMM

        Parameters
        ----------
        mol : Molecule
            The Molecule object containing the topology of the molecule
        prm : parmed.ParameterSet
            A parmed ParameterSet object containing the parameters of the molecule
        platform : str
            The platform on which to run the minimization ('CPU', 'CUDA')
        device : int
            If platform is 'CUDA' this defines which GPU device to use
        buildff : str
            The forcefield for which to build the Molecule to then minimize it with OpenMM

        Examples
        --------
        >>> from parameterize.parameterization.fftype import fftype
        >>> from moleculekit.molecule import Molecule

        >>> molFile = os.path.join(home('test-qm'), 'H2O2-90.mol2')
        >>> mol = Molecule(molFile)
        >>> prm, mol = fftype(mol, method='GAFF2')
        >>> mini = OMMMinimizer(mol, prm)
        >>> minimcoor = mini.minimize(mol.coords, restrained_dihedrals=[0, 1, 6, 12])
        """
        super().__init__()

        import simtk.openmm as mm

        if buildff == "AMBER":
            self.structure = self._get_prmtop(mol, prm)

        self.system = self.structure.createSystem()
        self.platform = mm.Platform.getPlatformByName(platform)
        self.platprop = (
            {"CudaPrecision": "mixed", "CudaDeviceIndex": device}
            if platform == "CUDA"
            else None
        )

    def _get_prmtop(self, mol, prm):
        from parameterize.parameterization.writers import (
            writeFRCMOD,
            getAtomTypeMapping,
        )
        from tempfile import TemporaryDirectory
        from subprocess import call
        from simtk.openmm import app

        with TemporaryDirectory() as tmpDir:
            frcFile = os.path.join(tmpDir, "mol.frcmod")
            mapping = getAtomTypeMapping(prm)
            writeFRCMOD(mol, prm, frcFile, typemap=mapping)
            mol2 = mol.copy()
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

    def minimize(self, coords, restrained_dihedrals=None, maxeval=None):
        from simtk import unit
        from simtk.openmm import app, PeriodicTorsionForce
        import simtk.openmm as mm
        from scipy.optimize import minimize

        forceidx = []
        if restrained_dihedrals:
            f = PeriodicTorsionForce()

            for dihedral in restrained_dihedrals:
                theta0 = dihedralAngle(coords[dihedral])
                f.addTorsion(
                    *tuple(map(int, dihedral)),
                    periodicity=1,
                    phase=theta0,
                    k=-10000 * unit.kilocalories_per_mole
                )

            fidx = self.system.addForce(f)
            forceidx.append(fidx)

        if coords.ndim == 3:
            coords = coords[:, :, 0]

        natoms = coords.shape[0]

        integrator = mm.LangevinIntegrator(0, 0, 0)
        sim = app.Simulation(
            self.structure.topology,
            self.system,
            integrator,
            self.platform,
            self.platprop,
        )

        def goalFunc(x):
            sim.context.setPositions(x.reshape((natoms, 3)) * unit.angstrom)
            state = sim.context.getState(getEnergy=True, getForces=True)
            energy = state.getPotentialEnergy().value_in_unit(
                unit.kilocalories_per_mole
            )
            forces = state.getForces(asNumpy=True).value_in_unit(
                unit.kilocalories_per_mole / unit.angstrom
            )
            grad = -forces.reshape(-1)
            return energy, grad

        force_tolerance = 0.1  # kcal/mol/A
        max_attempts = 50
        best_result = None
        best_force = np.inf
        for i in range(max_attempts):
            result = minimize(
                goalFunc,
                coords.reshape(-1),
                method="L-BFGS-B",
                jac=True,
                options={"ftol": 0, "gtol": force_tolerance},
            )
            max_force = np.abs(result.jac).max()

            if max_force < best_force:
                best_force = max_force
                best_result = result

            if max_force > force_tolerance:
                # Try to continue minimization by restarting the minimizer
                result = minimize(
                    goalFunc,
                    result.x,
                    method="L-BFGS-B",
                    jac=True,
                    options={"ftol": 0, "gtol": force_tolerance},
                )
                max_force = np.abs(result.jac).max()

            if max_force < best_force:
                best_force = max_force
                best_result = result

            if max_force <= force_tolerance:
                break

        if best_force > force_tolerance:
            logger.warning(
                "Did not manage to minimize structure to the desired force tolerance. Best minimized structure had a max force component {:.2f} kcal/mol/A. Threshold is {}".format(
                    best_force, force_tolerance
                )
            )

        minimized_coords = best_result.x.reshape((natoms, 3)).copy()

        if restrained_dihedrals:
            for fi in forceidx[::-1]:
                self.system.removeForce(fi)

        return minimized_coords


class CustomEnergyBasedMinimizer(Minimizer):
    def __init__(self, mol, calculator):
        super().__init__()
        self.opt = nlopt.opt(nlopt.LN_COBYLA, mol.coords.size)

        def objective(x, _):
            return float(
                calculator.calculate(
                    x.reshape((-1, 3, 1)), mol.element, units="kcalmol"
                )[0]
            )

        self.opt.set_min_objective(objective)

    def minimize(self, coords, restrained_dihedrals):
        if restrained_dihedrals is not None:
            for dihedral in restrained_dihedrals:
                indices = dihedral.copy()
                ref_angle = dihedralAngle(coords[indices, :, 0])

                def constraint(x, _):
                    coords = x.reshape((-1, 3))
                    angle = dihedralAngle(coords[indices])
                    return np.sin(0.5 * (angle - ref_angle))

                self.opt.add_equality_constraint(constraint)

        self.opt.set_xtol_abs(1e-3)  # Similar to Psi4 default
        self.opt.set_maxeval(1000 * self.opt.get_dimension())
        self.opt.set_initial_step(1e-3)
        return self.opt.optimize(coords.ravel()).reshape((-1, 3, 1))


class CustomQM(QMBase):
    """
    Imitation of QM calculations with custom class

    >>> import os
    >>> import numpy as np
    >>> from tempfile import TemporaryDirectory
    >>> from parameterize.home import home
    >>> from moleculekit.dihedral import dihedralAngle
    >>> from moleculekit.molecule import Molecule
    >>> from parameterize.qm.custom import CustomQM
    >>> from acemdai.calculator import AAICalculator

    Create a molecule
    >>> molFile = os.path.join(home('test-qm'), 'H2O2-90.mol2')
    >>> mol = Molecule(molFile)

    Create the AcemdAI calculator
    >>> networkfile = './mynet.pkl'
    >>> aceai = AAICalculator(networkfile=networkfile, maxatoms=26, maxneighs=50)

    Run a single-point energy and ESP calculation
    >>> with TemporaryDirectory() as tmpDir:
    ...     qm = CustomQM()
    ...     qm.calculator = aceai
    ...     qm.molecule = mol
    ...     qm.esp_points = np.array([[1., 1., 1.]])
    ...     qm.directory = tmpDir
    ...     result = qm.run()[0]
    4 elements | 10 element pairs | 384 features
    CUDA: Allocating features array (1, 4, 384)
    CUDA: Allocating gradient array (1, 4, 4, 384, 3)
    >>> qm # doctest: +ELLIPSIS
    <parameterize.qm.custom.CustomCalculator object at ...>
    >>> result # doctest: +ELLIPSIS
    <parameterize.qm.base.QMResult object at ...
    >>> result.errored
    False
    >>> result.energy # doctest: +ELLIPSIS
    -94970.499...
    >>> np.rad2deg(dihedralAngle(result.coords[[2, 0, 1, 3], :, 0])) # doctest: +ELLIPSIS
    89.99...

    Run a minimization
    >>> with TemporaryDirectory() as tmpDir:
    ...     qm = CustomQM()
    ...     qm.calculator = aceai
    ...     qm.molecule = mol
    ...     qm.optimize = True
    ...     qm.directory = tmpDir
    ...     result = qm.run()[0]
    4 elements | 10 element pairs | 384 features
    CUDA: Allocating features array (1, 4, 384)
    CUDA: Allocating gradient array (1, 4, 4, 384, 3)
    >>> result.energy # doctest: +ELLIPSIS
    -95173.433...
    >>> np.rad2deg(dihedralAngle(result.coords[[2, 0, 1, 3], :, 0])) # doctest: +ELLIPSIS
    125.993...

    Run a constrained minimization
    >>> with TemporaryDirectory() as tmpDir:
    ...     qm = CustomQM()
    ...     qm.calculator = aceai
    ...     qm.molecule = mol
    ...     qm.optimize = True
    ...     qm.restrained_dihedrals = np.array([[2, 0, 1, 3]])
    ...     qm.directory = tmpDir
    ...     result = qm.run()[0]
    4 elements | 10 element pairs | 384 features
    CUDA: Allocating features array (1, 4, 384)
    CUDA: Allocating gradient array (1, 4, 4, 384, 3)
    >>> result.energy # doctest: +ELLIPSIS
    -95170.800...
    >>> np.rad2deg(dihedralAngle(result.coords[[2, 0, 1, 3], :, 0])) # doctest: +ELLIPSIS
    89.99...
    """

    def __init__(self, verbose=True):
        super().__init__()
        self._verbose = verbose
        self._arg(
            "calculator",
            ":class: `Calculator`",
            "Calculator object",
            default=None,
            validator=None,
            required=True,
        )
        self._arg(
            "minimizer",
            ":class: `Minimizer`",
            "Minimizer object",
            default=None,
            validator=None,
        )

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

    def _completed(self, directory):
        return os.path.exists(os.path.join(directory, "data.pkl"))

    def retrieve(self):

        results = []
        for iframe in range(self.molecule.numFrames):
            self.molecule.frame = iframe

            directory = os.path.join(self.directory, "%05d" % iframe)
            os.makedirs(directory, exist_ok=True)
            pickleFile = os.path.join(directory, "data.pkl")
            molFile = os.path.join(directory, "mol.mol2")

            if self._completed(directory):
                with open(pickleFile, "rb") as fd:
                    result = pickle.load(fd)
                logger.info("Loading data from %s" % pickleFile)

            else:
                start = time.clock()

                result = QMResult()
                result.errored = False
                result.coords = self.molecule.coords[:, :, iframe : iframe + 1].copy()

                if self.optimize:
                    if self.minimizer is None:
                        self.minimizer = CustomEnergyBasedMinimizer(
                            self.molecule, self.calculator
                        )
                    result.coords = self.minimizer.minimize(
                        result.coords, self.restrained_dihedrals
                    ).reshape((-1, 3, 1))
                    mol = self.molecule.copy()
                    mol.frame = 0
                    mol.coords = result.coords
                    mol.write(molFile)

                result.energy = float(
                    self.calculator.calculate(
                        result.coords, self.molecule.element, units="kcalmol"
                    )[0]
                )
                result.dipole = [0, 0, 0]

                # if self.optimize:
                #    assert opt.last_optimum_value() == result.energy # A self-consistency test

                finish = time.clock()
                result.calculator_time = finish - start
                if self._verbose:
                    logger.info(
                        "Custom calculator calculation time: %f s"
                        % result.calculator_time
                    )

                with open(pickleFile, "wb") as fd:
                    pickle.dump(result, fd)

            results.append(result)

        return results


if __name__ == "__main__":

    import sys

    # TODO: Currently doctest is not working correctly, and qmml module is not made available either
    # import doctest
    #
    # if doctest.testmod().failed:
    #     sys.exit(1)
