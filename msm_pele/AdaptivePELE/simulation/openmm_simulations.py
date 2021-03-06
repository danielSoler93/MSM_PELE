"""
Helper functions to run with OpenMM, these  function could be methods of the
MDSimulation class however because of limitations imposed by the
multiprocessing they need to be functions
"""
from __future__ import absolute_import, division, print_function
import os
import sys
import time
import functools
import traceback
import simtk.openmm as mm
import simtk.openmm.app as app
import simtk.unit as unit
import numpy as np
from AdaptivePELE.constants import constants
from AdaptivePELE.utilities import utilities
from mdtraj.reporters.basereporter import _BaseReporter
from mdtraj.formats import XTCTrajectoryFile
try:
    FileNotFoundError
except NameError:
    FileNotFoundError = IOError
try:
    basestring
except NameError:
    basestring = str


def get_traceback(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as ex:
            ret = '#' * 60
            ret += "\nException caught:"
            ret += "\n"+'-'*60
            ret += "\n" + traceback.format_exc()
            ret += "\n" + '-' * 60
            ret += "\n" + "#" * 60
            print(sys.stderr, ret)
            sys.stderr.flush()
            raise ex

    return wrapper


class XTCReporter(_BaseReporter):
    """
        XTCReporter stores a molecular dynamics trajectory in the GROMACS xtc
        format

        :param file: Either an open XTCTrajectoryFile object to write to, or a string
            specifying the filename of a new XTC file to save the trajectory to.
        :type file: str, or :py:class:`XTCTrajectoryFile`
        :param reportInterval: The interval (in time steps) at which to write frames.
        :type reportInterval: int
        :param atomSubset: Only write a subset of the atoms, with these (zero based) indices
        to the file. If None, *all* of the atoms will be written to disk.
        :type atomSubset: arrray_like
        :param append: Whether to append the trajectory to a previously existing one
        :type append: bool
    """
    @property
    def backend(self):
        return XTCTrajectoryFile

    def __init__(self, file, reportInterval, atomSubset=None, append=False):
        if append:
            if isinstance(file, basestring):
                with self.backend(file, 'r') as f:
                    contents = f.read()
            elif isinstance(file, self.backend):
                raise ValueError("Currently passing an XTCTrajectoryFile in append mode is not supported, please pass a string with the filename")
            else:
                raise TypeError("I don't know how to handle %s" % file)
        super(XTCReporter, self).__init__(file, reportInterval, coordinates=True, time=True, cell=True, potentialEnergy=False,
                                          kineticEnergy=False, temperature=False, velocities=False, atomSubset=atomSubset)
        if append:
            self._traj_file.write(*contents)

    def report(self, simulation, state):
        """
            Generate a report

            :param simulation: simulation to generate the report for
            :type simulation: :py:class:`simtk.openmm.app.Simulation`
            :param state: current state of the simulation
            :type state: :py:class:`simtk.openmm.State`
        """
        if not self._is_intialized:
            self._initialize(simulation)
            self._is_intialized = True

        self._checkForErrors(simulation, state)
        args = ()
        kwargs = {}
        if self._coordinates:
            coordinates = state.getPositions(asNumpy=True)[self._atomSlice]
            coordinates = coordinates.value_in_unit(getattr(unit, self._traj_file.distance_unit))
            args = (coordinates,)

        if self._time:
            time_step = state.getTime()
            kwargs['time'] = time_step.value_in_unit(time_step.unit)
            kwargs['step'] = simulation.currentStep
        if self._cell:
            kwargs['box'] = state.getPeriodicBoxVectors(asNumpy=True).value_in_unit(getattr(unit, self._traj_file.distance_unit))
        self._traj_file.write(*args, **kwargs)
        # flush the file to disk. it might not be necessary to do this every
        # report, but this is the most proactive solution. We don't want to
        # accumulate a lot of data in memory only to find out, at the very
        # end of the run, that there wasn't enough space on disk to hold the
        # data.
        if hasattr(self._traj_file, 'flush'):
            self._traj_file.flush()


class CustomStateDataReporter(app.StateDataReporter):
    """
    New version of the StateDataReporter class that allows to append the md information to the last trajectory file
    It has two new parameters: (append: bool, and initialStep: int). The first controls if the header has to be written
    again and the second is the last step that was successfully done in the previous run.
    """
    # Added two new parameters append and intialsteps to properly handle the report file when the simulation is restarted
    # changed the name of the file and time parameters to avoid overriding
    # reserved names
    def __init__(self, file_name, reportInterval, step=False, time_sim=False, potentialEnergy=False, kineticEnergy=False, totalEnergy=False, temperature=False, volume=False, density=False,
                 progress=False, remainingTime=False, speed=False, elapsedTime=False, separator=',', systemMass=None, totalSteps=None, append=False, initialStep=0):

        # This new class doesn't properly support progress information. Because to do the restart it assumes that
        # the first column has the step information, which is True as long as the progress value is False.

        progress = False
        if isinstance(file_name, basestring):
            # if the file is passed as a string, wrap in a str call to avoid
            # problems between different versions of python
            file_name = str(file_name)

        app.StateDataReporter.__init__(self, file_name, reportInterval, step, time_sim, potentialEnergy, kineticEnergy, totalEnergy, temperature, volume, density,
                                       progress, remainingTime, speed, elapsedTime, separator, systemMass, totalSteps)
        self._append = append
        self.initialStep = initialStep
        self._initialClockTime = None
        self._initialSimulationTime = None
        self._initialSteps = None

    def report(self, simulation, state):
        """Generate a report.

        Parameters
        ----------
        simulation : Simulation
            The Simulation to generate a report for
        state : State
            The current state of the simulation
        """
        if not self._hasInitialized:
            self._initializeConstants(simulation)
            headers = self._constructHeaders()
            # Extra line added to avoid printing the header again when the simulation is restarted
            if not self._append:
                print('#"%s"' % ('"' + self._separator + '"').join(headers), file=self._out)
            try:
                self._out.flush()
            except AttributeError:
                pass
            self._initialClockTime = time.time()
            self._initialSimulationTime = state.getTime()
            self._initialSteps = simulation.currentStep
            self._hasInitialized = True

        # Check for errors.
        self._checkForErrors(simulation, state)

        # Query for the values
        values = self._constructReportValues(simulation, state)

        # Write the values.
        print(self._separator.join(str(v) for v in values), file=self._out)
        try:
            self._out.flush()
        except AttributeError:
            pass

    def _constructReportValues(self, simulation, state):
        # Modifies the first value which is the step number information
        values = super(CustomStateDataReporter, self)._constructReportValues(simulation, state)
        values[0] = values[0] + self.initialStep
        return values


@get_traceback
def runEquilibration(equilibrationFiles, reportName, parameters, worker):
    """
        Function that runs the whole equilibration process and returns the final pdb

        :param equilibrationFiles: tuple with the topology (prmtop) in the first position and the coordinates
        in the second (inpcrd)
        :type equilibrationFiles: tuple
        :param outputPDB: string with the pdb to save
        :type outputPDB: str
        :param parameters: Object with the parameters for the simulation
        :type parameters: :py:class:`/simulationrunner/SimulationParameters` -- SimulationParameters object
        :param worker: Number of the subprocess
        :type worker: int

        :returns: str -- a string with the outputPDB
    """
    prmtop, inpcrd = equilibrationFiles
    prmtop = app.AmberPrmtopFile(prmtop)
    inpcrd = app.AmberInpcrdFile(inpcrd)
    PLATFORM = mm.Platform_getPlatformByName(str(parameters.runningPlatform))
    if parameters.runningPlatform == "CUDA":
        platformProperties = {"Precision": "mixed", "DeviceIndex": getDeviceIndexStr(worker, parameters.devicesPerTrajectory, devicesPerReplica=parameters.maxDevicesPerReplica), "UseCpuPme": "false"}
    else:
        platformProperties = {}
    if worker == 0:
        utilities.print_unbuffered("Running %d steps of minimization" % parameters.minimizationIterations)
    simulation = minimization(prmtop, inpcrd, PLATFORM, parameters.constraintsMin, parameters, platformProperties)
    # Retrieving the state is expensive (especially when running on GPUs) so we
    # only called it once and then separate positions and velocities
    state = simulation.context.getState(getPositions=True, getVelocities=True)
    positions = state.getPositions()
    velocities = state.getVelocities()
    if worker == 0:
        utilities.print_unbuffered("Running %d steps of NVT equilibration" % parameters.equilibrationLengthNVT)
    simulation = NVTequilibration(prmtop, positions, PLATFORM, parameters.equilibrationLengthNVT, parameters.constraintsNVT, parameters, reportName, platformProperties, velocities=velocities)
    state = simulation.context.getState(getPositions=True, getVelocities=True)
    positions = state.getPositions()
    velocities = state.getVelocities()
    if worker == 0:
        utilities.print_unbuffered("Running %d steps of NPT equilibration" % parameters.equilibrationLengthNPT)
    simulation = NPTequilibration(prmtop, positions, PLATFORM, parameters.equilibrationLengthNPT, parameters.constraintsNPT, parameters, reportName, platformProperties, velocities=velocities)
    outputPDB = "%s_NPT.pdb" % reportName
    with open(outputPDB, 'w') as fw:
        app.PDBFile.writeFile(simulation.topology, simulation.context.getState(getPositions=True).getPositions(), fw)
    return outputPDB


@get_traceback
def minimization(prmtop, inpcrd, PLATFORM, constraints, parameters, platformProperties):
    """
    Function that runs a minimization of the system
    it uses the VerletIntegrator and applys to the heavy atoms of the
    protein and ligand.

    :param prmtop: OpenMM Topology object
    :param inpcrd: OpenMM Positions object
    :param PLATFORM: platform in which the minimization will run
    :type PLATFORM: str
    :param constraints: strength of the constrain (units: Kcal/mol)
    :type constraints: int
    :param parameters: Object with the parameters for the simulation
    :type parameters: :py:class:`/simulationrunner/SimulationParameters` -- SimulationParameters object
    :param platformProperties: Properties specific to the OpenMM platform
    :type platformProperties: dict

    :return: The minimized OpenMM simulation object
    """
    # Thermostat
    system = prmtop.createSystem(nonbondedMethod=app.PME,
                                 nonbondedCutoff=parameters.nonBondedCutoff * unit.angstroms, constraints=app.HBonds)
    # system.addForce(mm.AndersenThermostat(parameters.Temperature * unit.kelvin, 1 / unit.picosecond))
    integrator = mm.VerletIntegrator(parameters.timeStep * unit.femtoseconds)
    if constraints:
        # Add positional restraints to protein backbone
        force = mm.CustomExternalForce(str("k*((x-x0)^2+(y-y0)^2+(z-z0)^2)"))
        force.addGlobalParameter(str("k"), constraints * unit.kilocalories_per_mole / unit.angstroms ** 2)
        force.addPerParticleParameter(str("x0"))
        force.addPerParticleParameter(str("y0"))
        force.addPerParticleParameter(str("z0"))
        for j, atom in enumerate(prmtop.topology.atoms()):
            if (atom.name in ('CA', 'C', 'N', 'O') and atom.residue.name != "HOH") or (
                    atom.residue.name == parameters.ligandName and atom.element.symbol != "H"):
                force.addParticle(j, inpcrd.positions[j].value_in_unit(unit.nanometers))
        system.addForce(force)

    simulation = app.Simulation(prmtop.topology, system, integrator, PLATFORM, platformProperties=platformProperties)
    if inpcrd.boxVectors is not None:
        simulation.context.setPeriodicBoxVectors(*inpcrd.boxVectors)
    simulation.context.setPositions(inpcrd.positions)
    simulation.minimizeEnergy(maxIterations=parameters.minimizationIterations)
    return simulation


@get_traceback
def NVTequilibration(topology, positions, PLATFORM, simulation_steps, constraints, parameters, reportName, platformProperties, velocities=None):
    """
    Function that runs an equilibration at constant volume conditions.
    It uses the AndersenThermostat, the VerletIntegrator and
    applys constrains to the heavy atoms of the protein and ligands

    :param topology: OpenMM Topology object
    :param positions: OpenMM Positions object
    :param PLATFORM: platform in which the minimization will run
    :type PLATFORM: str
    :param simulation_steps: number of steps to run
    :type simulation_steps: int
    :param constraints: strength of the constrain (units: Kcal/mol)
    :type constraints: int
    :param parameters: Object with the parameters for the simulation
    :type parameters: :py:class:`/simulationrunner/SimulationParameters` -- SimulationParameters object
    :param platformProperties: Properties specific to the OpenMM platform
    :type platformProperties: dict
    :param velocities: OpenMM object with the velocities of the system. Optional, if velocities are not given,
    random velocities acording to the temperature will be used.

    :return: The equilibrated OpenMM simulation object
    """
    system = topology.createSystem(nonbondedMethod=app.PME,
                                   nonbondedCutoff=parameters.nonBondedCutoff * unit.angstroms,
                                   constraints=app.HBonds)
    system.addForce(mm.AndersenThermostat(parameters.Temperature * unit.kelvin, 1 / unit.picosecond))
    integrator = mm.VerletIntegrator(parameters.timeStep * unit.femtoseconds)
    if constraints:
        force = mm.CustomExternalForce(str("k*((x-x0)^2+(y-y0)^2+(z-z0)^2)"))
        force.addGlobalParameter(str("k"), constraints * unit.kilocalories_per_mole / unit.angstroms ** 2)
        force.addPerParticleParameter(str("x0"))
        force.addPerParticleParameter(str("y0"))
        force.addPerParticleParameter(str("z0"))
        for j, atom in enumerate(topology.topology.atoms()):
            if (atom.name in ('CA', 'C', 'N', 'O') and atom.residue.name != "HOH") or (atom.residue.name == parameters.ligandName and atom.element.symbol != "H"):
                force.addParticle(j, positions[j].value_in_unit(unit.nanometers))
        system.addForce(force)
    simulation = app.Simulation(topology.topology, system, integrator, PLATFORM, platformProperties=platformProperties)
    simulation.context.setPositions(positions)
    if velocities:
        simulation.context.setVelocities(velocities)
    else:
        simulation.context.setVelocitiesToTemperature(parameters.Temperature * unit.kelvin, 1)
    reportFile = "%s_report_NVT" % reportName
    simulation.reporters.append(CustomStateDataReporter(reportFile, parameters.reporterFreq, step=True,
                                                        potentialEnergy=True, temperature=True, time_sim=True,
                                                        volume=True, remainingTime=True, speed=True,
                                                        totalSteps=parameters.equilibrationLengthNVT, separator="\t"))
    simulation.step(simulation_steps)
    return simulation


@get_traceback
def NPTequilibration(topology, positions, PLATFORM, simulation_steps, constraints, parameters, reportName, platformProperties, velocities=None):
    """
    Function that runs an equilibration at constant pressure conditions.
    It uses the AndersenThermostat, the VerletIntegrator, the MonteCarlo Barostat and
    apply's constrains to the backbone of the protein and to the heavy atoms of the ligand

    :param topology: OpenMM Topology object
    :param positions: OpenMM Positions object
    :param PLATFORM: platform in which the minimization will run
    :type PLATFORM: str
    :param simulation_steps: number of steps to run
    :type simulation_steps: int
    :param constraints: strength of the constrain (units: Kcal/mol)
    :type constraints: int
    :param parameters: Object with the parameters for the simulation
    :type parameters: :py:class:`/simulationrunner/SimulationParameters` -- SimulationParameters object
    :param platformProperties: Properties specific to the OpenMM platform
    :type platformProperties: dict
    :param velocities: OpenMM object with the velocities of the system. Optional, if velocities are not given,
    random velocities acording to the temperature will be used.

    :return: The equilibrated OpenMM simulation object
    """
    system = topology.createSystem(nonbondedMethod=app.PME,
                                   nonbondedCutoff=parameters.nonBondedCutoff * unit.angstroms,
                                   constraints=app.HBonds)
    system.addForce(mm.AndersenThermostat(parameters.Temperature * unit.kelvin, 1 / unit.picosecond))
    integrator = mm.VerletIntegrator(parameters.timeStep * unit.femtoseconds)
    system.addForce(mm.MonteCarloBarostat(1 * unit.bar, parameters.Temperature * unit.kelvin))
    if constraints:
        force = mm.CustomExternalForce(str("k*((x-x0)^2+(y-y0)^2+(z-z0)^2)"))
        force.addGlobalParameter(str("k"), constraints * unit.kilocalories_per_mole / unit.angstroms ** 2)
        force.addPerParticleParameter(str("x0"))
        force.addPerParticleParameter(str("y0"))
        force.addPerParticleParameter(str("z0"))
        for j, atom in enumerate(topology.topology.atoms()):
            if atom.name == 'CA' or (atom.residue.name == parameters.ligandName and atom.element.symbol != "H"):
                force.addParticle(j, positions[j].value_in_unit(unit.nanometers))
        system.addForce(force)
    simulation = app.Simulation(topology.topology, system, integrator, PLATFORM, platformProperties=platformProperties)
    simulation.context.setPositions(positions)
    if velocities:
        simulation.context.setVelocities(velocities)
    else:
        simulation.context.setVelocitiesToTemperature(parameters.Temperature * unit.kelvin, 1)
    reportFile = "%s_report_NPT" % reportName
    simulation.reporters.append(CustomStateDataReporter(reportFile, parameters.reporterFreq, step=True,
                                                        potentialEnergy=True, temperature=True, time_sim=True,
                                                        volume=True, remainingTime=True, speed=True,
                                                        totalSteps=parameters.equilibrationLengthNPT, separator="\t"))
    simulation.step(simulation_steps)
    return simulation


@get_traceback
def runProductionSimulation(equilibrationFiles, workerNumber, outputDir, seed, parameters, reportFileName, checkpoint, ligandName, replica_id, trajsPerReplica, restart=False):
    """
    Functions that runs the production run at NVT conditions.
    If a boxRadius is defined in the parameters section, a Flat-bottom harmonic restrains will be applied between
    the protein and the ligand

    :param equilibrationFiles: Tuple with the paths for the Amber topology file (prmtop) and the pdb for the system
    :type equilibrationFiles: Tuple
    :param workerNumber: Number of the subprocess
    :type workerNumber: int
    :param outputDir: path to the directory where the output will be written
    :type outputDir: str
    :param seed: Seed to use to generate the random numbers
    :type seed: int
    :param parameters: Object with the parameters for the simulation
    :type parameters: :py:class:`/simulationrunner/SimulationParameters` -- SimulationParameters object
    :param reportFileName: Name for the file where the energy report will be written
    :type reportFileName: str
    :param checkpoint: Path to the checkpoint from where the production run will be restarted (Optional)
    :type checkpoint: str
    :param ligandName: Code Name for the ligand
    :type ligandName: str
    :param replica_id: Id of the replica running
    :type replica_id: int
    :param trajsPerReplica: Number of trajectories per replica
    :type trajsPerReplica: int
    :param restart: Whether the simulation run has to be restarted or not
    :type restart: bool

    """
    deviceIndex = workerNumber
    workerNumber += replica_id*trajsPerReplica + 1
    prmtop, pdb = equilibrationFiles
    prmtop = app.AmberPrmtopFile(prmtop)
    trajName = os.path.join(outputDir, constants.AmberTemplates.trajectoryTemplate % (workerNumber, parameters.format))
    stateReporter = os.path.join(outputDir, "%s_%s" % (reportFileName, workerNumber))
    checkpointReporter = os.path.join(outputDir, constants.AmberTemplates.CheckPointReporterTemplate % workerNumber)
    lastStep = getLastStep(stateReporter)
    simulation_length = parameters.productionLength - lastStep
    # if the string is unicode the PDBReaders fails to read the file (this is
    # probably due to the fact that openmm was built with python2 in my
    # computer, will need to test thoroughly with python3)
    pdb = app.PDBFile(str(pdb))
    PLATFORM = mm.Platform_getPlatformByName(str(parameters.runningPlatform))
    if parameters.runningPlatform == "CUDA":
        platformProperties = {"Precision": "mixed", "DeviceIndex": getDeviceIndexStr(deviceIndex, parameters.devicesPerTrajectory, devicesPerReplica=parameters.maxDevicesPerReplica), "UseCpuPme": "false"}
    else:
        platformProperties = {}
    system = prmtop.createSystem(nonbondedMethod=app.PME,
                                 nonbondedCutoff=parameters.nonBondedCutoff * unit.angstroms,
                                 constraints=app.HBonds, removeCMMotion=True)
    system.addForce(mm.AndersenThermostat(parameters.Temperature * unit.kelvin, 1 / unit.picosecond))
    integrator = mm.VerletIntegrator(parameters.timeStep * unit.femtoseconds)
    system.addForce(mm.MonteCarloBarostat(1 * unit.bar, parameters.Temperature * unit.kelvin))
    if parameters.boxRadius:
        group_ligand = []
        group_protein = []
        for atom in prmtop.topology.atoms():
            if atom.residue.name == ligandName:
                group_ligand.append(atom.index)
            elif atom.residue.name not in ("HOH", "Cl-", "Na+"):
                group_protein.append(atom.index)
        # Harmonic flat-bottom restrain for the ligand
        group_ligand = np.array(group_ligand)
        group_protein = np.array(group_protein)
        force = mm.CustomCentroidBondForce(2, 'step(distance(g1,g2)-r) * (k/2) * (distance(g1,g2)-r)^2')
        force.addGlobalParameter("k", 5.0 * unit.kilocalories_per_mole / unit.angstroms ** 2)
        force.addGlobalParameter("r", parameters.boxRadius * unit.angstroms)
        force.addGroup(group_protein)
        force.addGroup(group_ligand)
        force.addBond([0, 1], []) # the first parameter is the list of indexes of the groups, the second is the list of perbondparameters
        system.addForce(force)
    simulation = app.Simulation(prmtop.topology, system, integrator, PLATFORM, platformProperties=platformProperties)
    simulation.context.setPositions(pdb.positions)

    if restart:
        with open(str(checkpoint), 'rb') as check:
            simulation.context.loadCheckpoint(check.read())
            stateData = open(str(stateReporter), "a")
    else:
        simulation.context.setVelocitiesToTemperature(parameters.Temperature * unit.kelvin, seed)
        stateData = open(str(stateReporter), "w")

    if parameters.format == "xtc":
        simulation.reporters.append(XTCReporter(str(trajName), parameters.reporterFreq, append=restart))
    elif parameters.format == "dcd":
        simulation.reporters.append(app.DCDReporter(str(trajName), parameters.reporterFreq, append=restart, enforcePeriodicBox=True))

    simulation.reporters.append(app.CheckpointReporter(str(checkpointReporter), parameters.reporterFreq))
    simulation.reporters.append(CustomStateDataReporter(stateData, parameters.reporterFreq, step=True,
                                                        potentialEnergy=True, temperature=True, time_sim=True,
                                                        volume=True, remainingTime=True, speed=True,
                                                        totalSteps=parameters.productionLength, separator="\t",
                                                        append=restart, initialStep=lastStep))
    if workerNumber == 1:
        frequency = min(10 * parameters.reporterFreq, parameters.productionLength)
        simulation.reporters.append(app.StateDataReporter(sys.stdout, frequency, step=True))
    simulation.step(simulation_length)
    stateData.close()


def getLastStep(reportfile):
    """
    Function that given a MD report file extracts the last step that was properly done

    :param reportfile: reportfile with the previous md simulation
    :type reportfile: str
    :return: The number of the last step that was successfully done
    """
    try:
        with open(reportfile, "r") as inp:
            report = inp.read()
        last_step = report.split("\n")[-2].split("\t")[0]
    except FileNotFoundError:
        last_step = 0
    return int(last_step)


def getDeviceIndexStr(deviceIndex, devicesPerTraj, devicesPerReplica=None):
    """
        Create a string to pass to OpenMM platform to select the resources to use

        :param deviceIndex: Index of the trajectory in the replica
        :type deviceIndex: int
        :param devicesPerTraj: Number of devices to use per trajectory
        :type devicesPerTraj: int
        :param devicesPerReplica: Number of maximum devices to use per replica
        :type devicesPerReplica: int

        :returns: str -- String that tells OpenMM how to use the resources
    """
    if devicesPerReplica is not None:
        devices = [d % devicesPerReplica for d in range(deviceIndex, deviceIndex+devicesPerTraj)]
    else:
        devices = range(deviceIndex, deviceIndex+devicesPerTraj)
    return ",".join(str(x) for x in devices)
