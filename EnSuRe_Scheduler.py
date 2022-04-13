import math
import copy
import random

class EnSuRe_Scheduler:
    # init
    def __init__(self, k, frame, time_step, m_pri, lp_hp_ratio, log_debug):
        """
        Class constructor (__init__).

        k: number of faults the system can support
        frame: size of the frame, in ms
        time_step: fidelity of each time step for the scheduler/task execution times, in ms
        m_pri: number of primary (LP) cores
        log_debug: whether to print logging statements
        """
        # application parameters
        self.k = k
        self.frame = frame  # total duration
        self.time_step = time_step

        self.precision_dp = -round(math.log(self.time_step, 10))

        # system parameters
        self.m_pri = m_pri  # no. primary cores
        self.lp_hp_ratio = lp_hp_ratio  # LP:HP speed ratio

        # scheduler variables
        self.pri_schedule = dict()
        self.deadlines = None   # an array of the task deadlines, ordered in increasing order
        self.backup_start = []  # an array of backup start times, one per time window
        self.backup_list = []   # an array of backup lists, one list per time window

        # logging
        self.log_debug = log_debug  # whether to print log statements or not

    # class helper functions
    def getTaskDeadline(task):
        """
        Helper function for sorting tasks in order of their deadlines.
        """
        return task.getDeadline()

    def getTaskWQ(task):
        """
        Helper function for sorting tasks in order of the size of their workload-quota.
        """
        return task.getWorkloadQuota(len(task.workload_quota)-1)

    def roundUpTimeStep(self, value):
        out = round(math.ceil(value/self.time_step) * self.time_step, self.precision_dp)
        if out < self.time_step:
            out = self.time_step
        return out

    def remove_from_backup_list(self, idx, taskId, sim_time):
        """
        Given a task id, remove its corresponding task from the backup_list for the current time-window.
        To be called when a task (either its primary or backup copy) completes execution successfully.

        idx: the current time-window
        taskId: id of the task to be removed
        """
        # remove task from backup list
        self.backup_list[idx] = [b for b in self.backup_list[idx] if b.getId() != taskId]
        # update size of BB-overloading window
        self.update_BB_overloading(idx, sim_time)

    def update_BB_overloading(self, idx, sim_time):
        """
        Update backup_start with the new size of the BB-overloading window for the current time-window.
        Up to k tasks will be reserved for BB-overloading.

        idx: the current time window
        """
        # compute BB-overloading window size
        reserve_cap = 0
        l = min(self.k, len(self.backup_list[idx]))
        for z in range(l):
            reserve_cap += self.backup_list[idx][z].getBackupWorkloadQuota(idx) # NOTE: modification to schedule by backup workload quota

        # reserve reserve_cap units of backup slots
        new_backup_start = self.deadlines[idx] - reserve_cap
        if len(self.backup_start) <= idx:
            self.backup_start.append(new_backup_start)
        else:
            self.backup_start[idx] = max(sim_time, new_backup_start)

    # Function to generate schedule
    def generate_schedule(self, tasksList):
        """
        Try to generate a schedule for the given task set. This follows the pseudo-code of the EnSuRe algorithm from the paper.
        Returns True if a feasible schedule is generated successfully, or False if no feasible schedule can be generated.
        
        tasksList: the task set to generate a schedule for.
        """
        # 1. Sort tasks in increasing order of deadlines to obtain deadline sequence
        tasksList.sort(reverse=False, key=EnSuRe_Scheduler.getTaskDeadline)
        self.deadlines = []
        [self.deadlines.append(task.getDeadline()) for task in tasksList if task.getDeadline() not in self.deadlines]   # NOTE: removes duplicate deadlines

        # 2. In each time window, schedule primary tasks onto the LP core
        for i in range(len(self.deadlines)): # each task in the list is the next deadline
            # i. calculate time window
            if i == 0:  # first deadline
                time_window = self.deadlines[i]
                start_window = 0
            else:
                time_window = self.deadlines[i] - self.deadlines[i-1]
                start_window = self.deadlines[i-1]

            # ii. for each task, calculate workload-quota
            total_wq = 0
            for task in tasksList:
                wq = self.roundUpTimeStep(task.getWeight() * time_window)
                task.setWorkloadQuota(wq)
                bwq = self.roundUpTimeStep(self.lp_hp_ratio * task.getWeight() * time_window)
                task.setBackupWorkloadQuota(bwq)
                total_wq += wq

            # iii. check if system-wide capacity >= total workload-quota for all running tasks
            if total_wq <= time_window * self.m_pri: # equation satisfied, feasible schedule

                # iv. execute tasks in the primary cores as per workload-quota
                tasksA = copy.deepcopy(tasksList)
                tasksA.sort(reverse=True, key=EnSuRe_Scheduler.getTaskWQ)
                # keep track of cores' schedules
                currPriCore = 0
                pri_cores = [start_window] * self.m_pri
                self.pri_schedule[i] = {}
                for t in tasksA:
                    lp_executionTime = t.getWorkloadQuota(i)
                    # attempt to schedule onto this core
                    counter = 0
                    while pri_cores[currPriCore] + lp_executionTime > start_window + time_window:   # cannot be scheduled onto this core
                        # go to another core
                        currPriCore += 1
                        if currPriCore >= self.m_pri:
                            currPriCore = 0
                        counter += 1
                        if counter > self.m_pri:    # not schedulable, exit
                            print("Unable to schedule tasks when trying to assign to LP cores")
                            return False

                    # schedule onto this core
                    self.pri_schedule[i][(pri_cores[currPriCore], currPriCore)] = t # 2D array: [deadline] [(start_time, core_id)]
                    t.setStartTime(pri_cores[currPriCore])
                    pri_cores[currPriCore] += lp_executionTime
                    # go to another core
                    currPriCore += 1
                    if currPriCore >= self.m_pri:
                        currPriCore = 0

                    # v. remove task from tasksList if workload-quota completes
                    if self.deadlines[i] == t.getDeadline():    # true if task would be completed in this time window
                        for t_toRemove in tasksList:
                            if t_toRemove.getId() == t.getId():
                                tasksList.remove(t_toRemove)
                                break

                # sort the primary schedule by time
                self.pri_schedule[i] = dict(sorted(self.pri_schedule[i].items(), key=lambda key: key[0]))


                # vi. calculate t_slack (start of slack time) and available slack for each core
                available_slack = [0] * self.m_pri
                t_slack = pri_cores[0]
                for m in range(self.m_pri):
                    available_slack[m] = pri_cores[m]
                    if pri_cores[m] < t_slack:
                        t_slack = pri_cores[m]

                # vii. calculate urgency factor of tasks - actually, this is the same as the sorted task list, right?

                # viii. schedule optional portion of the task

                # ix. create backup list
                tempList = tasksA.copy()    # NOTE: taskA is used as it still contains the task that would get completed in this time window
                #tempList.sort(reverse=True, key=EnSuRe_Scheduler.getTaskWQ) # NOTE: modification to schedule by backup workload quota
                self.backup_list.append(tempList)

                # x. compute BB-overloading window size
                self.update_BB_overloading(i, 0)
                

            else:   ## if not schedulable, exit
                print("Unable to schedule tasks, WQ < time_window")
                return False

        # Generated schedule successfully
        return True

    def print_schedule(self):
        """
        Print the generated schedule to the console log.
        """
        print("Schedule:")
        #for i in range(len(self.deadlines)): # each task in the list is the next deadline 
        print(" Primary Tasks")
        for i in self.pri_schedule.keys():  # deadline
            for key in self.pri_schedule[i].keys(): # (start_time, core_id)
                print("  LP Core {0}, {1} ms, Task {2}".format(key[1], key[0], self.pri_schedule[i][key].getId()))

        print(" Backup Tasks")
        for i in range(len(self.deadlines)): # each task in the list is the next deadline 
            print("  For time window {0}: {1} ms".format(i, self.backup_start[i]))

    def simulate(self, lp_cores, hp_core):
        sim_time = 0
        for i in range(len(self.deadlines)):
            # reset fault encountering for tasks first
            for t in self.pri_schedule[i].values():
                t.resetEncounteredFault()

            # 1. Calculate the times when faults occur
            self.generate_fault_occurrences(i)

            # 2. Simulate time steps
            lp_assignedTask = [None] * len(lp_cores)
            hp_assignedTask = None
            key = list(self.pri_schedule[i].keys())[0]
            keyIdx = 0
            while sim_time <= self.deadlines[i]:
                # i. increment active durations
                for lp in range(len(lp_assignedTask)):
                    if not lp_assignedTask[lp] is None:
                        lp_cores[lp].update_active_duration(self.time_step)
                if not hp_assignedTask is None:
                    hp_core.update_active_duration(self.time_step)

                # ii. if a primary task has completed, unassign it from core
                for lp in range(len(lp_assignedTask)):
                    if not lp_assignedTask[lp] is None:
                        if sim_time >= lp_assignedTask[lp].getStartTime() + lp_assignedTask[lp].getWorkloadQuota(i):
                            # if it is a task that shouldn't have encountered an error
                            if not lp_assignedTask[lp].getEncounteredFault():
                                # iii. remove from backup list
                                self.remove_from_backup_list(i, lp_assignedTask[lp].getId(), sim_time)
                                # if its backup task is already executing and it completed (i.e. did not encounter a fault), cancel the backup task
                                if not hp_assignedTask is None and hp_assignedTask.getId() == lp_assignedTask[lp].getId():
                                    hp_assignedTask = None

                            # unassign from core
                            lp_assignedTask[lp] = None

                # iv. if a backup task has completed, remove it from backup core
                if not hp_assignedTask is None:
                    if self.backup_list[i] and sim_time >= hp_assignedTask.getBackupStartTime() + hp_assignedTask.getBackupWorkloadQuota(i):
                        #remove from backup list
                        self.remove_from_backup_list(i, hp_assignedTask.getId(), sim_time)

                        # unassign from core
                        hp_assignedTask = None

                # v. update primary task assignment to cores
                while (keyIdx < len(self.pri_schedule[i])) and sim_time >= key[0]:
                    # it actually completed execution, but floating point's a bitch
                    if not lp_assignedTask[key[1]] is None and lp_assignedTask[key[1]].getId() != self.pri_schedule[i][key].getId():
                        # if it is a task that shouldn't have encountered an error
                        if not lp_assignedTask[key[1]].getEncounteredFault():
                            # iii. remove from backup list
                            self.remove_from_backup_list(i, lp_assignedTask[key[1]].getId(), sim_time)
                            # if its backup task is already executing and it completed (i.e. did not encounter a fault), cancel the backup task
                            if hp_assignedTask is not None and hp_assignedTask.getId() == lp_assignedTask[key[1]].getId():
                                hp_assignedTask = None

                    if lp_assignedTask[key[1]] is None or lp_assignedTask[key[1]].getId() != self.pri_schedule[i][key].getId():
                        lp_assignedTask[key[1]] = self.pri_schedule[i][key]
                        lp_assignedTask[key[1]].setStartTime(sim_time)

                    keyIdx += 1
                    if keyIdx >= len(self.pri_schedule[i]):
                        key = None
                    else:
                        key = list(self.pri_schedule[i].keys())[keyIdx]

                # vi. update task assignment to backup core
                if sim_time >= self.backup_start[i]:
                    if self.backup_list[i]:
                        # task hasn't started on backup core yet
                        if hp_assignedTask is None or hp_assignedTask.getId() != self.backup_list[i][0].getId():
                            hp_assignedTask = self.backup_list[i][0]
                            hp_assignedTask.setBackupStartTime(sim_time)
                    else:
                        hp_assignedTask = None

                sim_time += self.time_step

        # 3. Calculate energy consumption of the system from active/idle durations
        for lpcore in lp_cores:
            # i. calculate active energy consumption for this core
            activeConsumption = lpcore.energy_consumption_active(lpcore.get_active_duration())
            lpcore.update_energy_consumption(activeConsumption)
            # ii. calculate idle energy consumption for this core
            idleConsumption = lpcore.energy_consumption_idle(self.frame - lpcore.get_active_duration())
            lpcore.update_energy_consumption(idleConsumption)
        
        # iii. calculate active energy consumption for HP core
        hp_activeConsumption = hp_core.energy_consumption_active(hp_core.get_active_duration())
        hp_core.update_energy_consumption(hp_activeConsumption)
        # iv. calculate idle energy consumption for HP core
        hp_idleConsumption = hp_core.energy_consumption_idle(self.frame - hp_core.get_active_duration())
        hp_core.update_energy_consumption(hp_idleConsumption)



    def simulate_old(self, lp_cores, hp_core):
        # 0. For each time window, simulate:
        for i in range(len(self.deadlines)):

            # reset fault encountering for tasks first
            for t in self.pri_schedule[i].values():
                t.resetEncounteredFault()

            # 1. Calculate the times when faults occur
            self.generate_fault_occurrences(i)

            # 2. "Simulate" execution/completion times of the tasks (TODO: take note of overlapping with backup core)
            num_faults_left = min(self.k, len(self.pri_schedule[i]))
            for key in self.pri_schedule[i].keys(): # in sequence of execution start time

                curr_time = key[0]  # the curr "simulation" time
                task = self.pri_schedule[i][key]    # reference to task

                # 0. at this time step, check if curr_time is greater than the completion time of a backup task, to update backup_list and BB-overloading window
                if curr_time > self.backup_start[i]:    # we are within a backup execution time, proceed to further checks
                    while (curr_time > (self.backup_start[i] + self.backup_list[i][0].getBackupWorkloadQuota(i))):  # the backup task of a faulty task has completed
                        #print("{0}, {1}, {2}".format(curr_time, self.backup_start[i], self.backup_list[i][0].getBackupWorkloadQuota(i)))
                        if self.backup_list[i][0].getEncounteredFault():
                            hp_core.update_active_duration(self.backup_list[i][0].getBackupWorkloadQuota(i))
                            self.backup_list[i][0].completed = True
                        elif not self.backup_list[i][0].completed:
                            # Backup task completed before primary task did
                            for v in self.pri_schedule[i].values():
                                if self.backup_list[i][0].getId() == v.getId():
                                    #print("id: " + str(self.backup_list[i][0].getId()))
                                    v.setCompletionTime(self.backup_start[i] + v.getBackupWorkloadQuota(i))
                                    v.completed = True
                                    break
                            hp_core.update_active_duration(self.backup_list[i][0].getBackupWorkloadQuota(i))
                            num_faults_left += 1

                        # NOTE: backup core's active duration for this task already accounted for when the task fails
                        # update backup execution list
                        self.remove_from_backup_list(i, self.backup_list[i][0].getId())

                        # TEMP: just a checker
                        num_faults_left -= 1

                # if task encountered fault, updating energy consumption for HP core is straightforward
                #if task.getEncounteredFault():
                    # ii. update active duration for the execution time on HP core
                    # NOTE: removing from backup_list will only be done in a time step after this
                    #hp_core.update_active_duration(task.getBackupWorkloadQuota(i))
                    ##task.completed = True

                # 1. work on primary task
                if not task.getEncounteredFault() and not task.completed:
                    # i. update active duration for the execution time on LP core
                    lp_cores[key[1]].update_active_duration(task.getWorkloadQuota(i))

                    # check for overlap with backup execution
                    completion_time = curr_time + task.getWorkloadQuota(i)  # the time the task completes
                    if self.log_debug:
                        pass
                        #print("Completion time: {0}".format(completion_time))
                    task.setCompletionTime(completion_time)
                    task.completed = True
                    # check if task's primary copy has overlap with backup copy
                    if completion_time >= self.backup_start[i]:    # if the task completes after backup_start, there is a chance of overlap
                        # get start time of task's backup copy to calculate the overlap
                        backup_start_time = self.backup_start[i]
                        for backup_copy in self.backup_list[i]:
                            if task.getId() == backup_copy.getId():
                                break
                            else:
                                backup_start_time += backup_copy.getBackupWorkloadQuota(i)

                        # check for overlap
                        if completion_time > backup_start_time:  # there is an overlap
                            backup_overlap = completion_time - backup_start_time     # calculate the overlap duration
                            # i. set the amount of time HP spends on this task as the overlap execution time
                            #task.setHPExecutedDuration(backup_overlap)
                            # ii. update active duration for the execution time on HP core
                            hp_core.update_active_duration(backup_overlap)

                    # update backup execution list
                    self.remove_from_backup_list(i, task.getId())
                
            # TEMP: just to check that tasks completed successfully
            #print("num_faults_left: {0} for window {1}, left in backup: {2}".format(num_faults_left, i, len(self.backup_list[i])))
            if num_faults_left > 0:
                if num_faults_left < len(self.backup_list[i]):
                    print("Faults were not resolved properly. num_faults = {0}, backup_list length = {1}".format(num_faults_left, len(self.backup_list[i])))
                elif num_faults_left > len(self.backup_list[i]):
                    print("Some tasks cannot finish executing.")
                else:   # it is fine
                    if self.log_debug:
                        print("Alls gud: {0}".format(i))
                    self.backup_list[i].clear()
            elif num_faults_left < 0:
                print("Negative, whoopps")

        # 3. Calculate energy consumption of the system from active/idle durations
        for lpcore in lp_cores:
            # i. calculate active energy consumption for this core
            activeConsumption = lpcore.energy_consumption_active(lpcore.get_active_duration())
            lpcore.update_energy_consumption(activeConsumption)
            # ii. calculate idle energy consumption for this core
            idleConsumption = lpcore.energy_consumption_idle(self.frame - lpcore.get_active_duration())
            lpcore.update_energy_consumption(idleConsumption)
        
        # iii. calculate active energy consumption for HP core
        hp_activeConsumption = hp_core.energy_consumption_active(hp_core.get_active_duration())
        hp_core.update_energy_consumption(hp_activeConsumption)
        # iv. calculate idle energy consumption for HP core
        hp_idleConsumption = hp_core.energy_consumption_idle(self.frame - hp_core.get_active_duration())
        hp_core.update_energy_consumption(hp_idleConsumption)


    def generate_fault_occurrences(self, idx):
        """
        Generate the times at which faults will occur in this time-window, and mark the affected tasks to have encountered a fault.
        k faults will be generated. It is assumed that each task can only encounter one fault.
        If the number of tasks in this time-window is smaller than k, then a fault will be generated for all tasks in this time-window.

        The procedure for generating a fault:
        1. Randomly sample a discrete time step within the time window.
        2. Convert the discrete time step into the actual simulation time.
        3. Check if the generated fault time is valid, by seeing if it occurs during the execution time of a task, and the task is not already marked to have encountered a fault.
        4. If the generated fault time is not valid, repeat steps 1-3.
        5. Repeat steps 1-4 for k times or number of tasks in this time window, whichever is smaller.

        idx: the current time-window
        """
        l = min(self.k, len(self.pri_schedule[idx]))

        if idx == 0:  # first deadline
            time_window = self.deadlines[idx]
            start_window = 0
        else:
            time_window = self.deadlines[idx] - self.deadlines[idx-1]
            start_window = self.deadlines[idx-1]

        #  randomly generate the time occurrence of k faults
        fault_times = []
        faulty_tasks = []
        for f in range(l):
            # randomly choose a time for the fault to occur
            fault_time = None
            # randomly generate until a valid fault_time for fault to occur is obtained
            while fault_time is None:
                # randomly generate a time for the fault to occur
                rand = random.randint(0, time_window / self.time_step)  # randint [0, time_window / self.time_step) generates the random timestep it occurs
                fault_time = (self.time_step * rand) + start_window # get the actual time of the fault in ms
                # check if time step is valid
                for key in self.pri_schedule[idx].keys():
                    task = self.pri_schedule[idx][key]
                    if fault_time >= key[0] and fault_time <= key[0] + task.getWorkloadQuota(idx):
                        # if task already has a fault, skip it
                        if not task.getEncounteredFault():
                            # calculate the time step where fault occurred relative to the task start time
                            relative_fault_time = fault_time - key[0]
                            # mark task as having a fault
                            task.setEncounteredFault(idx, relative_fault_time)

                            # add it to list of time steps that a fault occurs
                            fault_times.append(fault_time)
                            faulty_tasks.append(task.getId())
                            break

                else:   # the time step is after all the tasks arranged
                    fault_time = None

        return faulty_tasks