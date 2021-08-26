# checkmark.py
# THe following file contains the class for costing scenarios using the
# checkmark model

import numpy as np
import pandas as pd
import os, sys, time
import re
import subprocess
import warnings
from os import path
import shutil
from mpi4py import MPI

from powerscenarios.costs.abstract_fidelity import AbstractCostingFidelity
from exago.opflow import OPFLOW
import os
from exago import config

class ExaGO_Lib(AbstractCostingFidelity):
    """
    This class contains the wrapper for calling OPFLOW from within Powerscenaios
    """
    def __init__(self,
                 n_scenarios, # Number of scenarios we actually want in our final csv file
                 n_periods,
                 loss_of_load_cost,
                 spilled_wind_cost,
                 scenarios_df,
                 p_bin,
                 total_power_t0,
                 WTK_DATA_PRECISION=6,
                 nscen_priced=1):

        AbstractCostingFidelity.__init__(self,
                                         n_scenarios,
                                         n_periods,
                                         loss_of_load_cost,
                                         spilled_wind_cost,
                                         scenarios_df,
                                         p_bin,
                                         total_power_t0,
                                         WTK_DATA_PRECISION=6)

        self.grid_name = "ACTIVSg200"
        self.opflow_options_dict = {'opflow_solver' : 'IPOPT',
                                    'matpower_file' : "{0}.m".format(self.grid_name),
                                   }
        self.sopflow_options_dict = {'nscenarios' : nscen_priced, # This needs to be a string as the function runs a bash command
                                    }

        self._create_ego_object()

        return

    def _create_ego_object(self):
        # Data files for creating the file based exago object
        network_file = "/Users/kpanda/UserApps/powerscenarios/data/grid-data/{0}/case_{0}.m".format(self.grid_name)
        grid_aux_file = "/Users/kpanda/UserApps/powerscenarios/data/grid-data/{0}/{0}.aux".format(self.grid_name)
        load_dir = "/Users/kpanda/UserApps/powerscenarios/data/load-data"
        real_load_file = "/Users/kpanda/UserApps/powerscenarios/data/load-data/{0}_loadP.csv".format(self.grid_name)
        reactive_load_file = "/Users/kpanda/UserApps/powerscenarios/data/load-data/{0}_loadQ.csv".format(self.grid_name)

        self.ego = ExaGO(network_file, load_dir, self.grid_name, real_load_file, reactive_load_file)
        self.ego._cleanup() # Lets clean up the file based implementation.

        return


    def compute_scenario_cost(self,
                              actuals_df,
                              binned_scenarios_df,
                              start_time,
                              random_seed=np.random.randint(2 ** 31 - 1)):

        stop_time = start_time # For now
        my_mpi_rank = self.ego.comm.Get_rank()
        comm_size = self.ego.comm.Get_size()

        # Create Persistence Forecast
        persistence_wind_fcst_df = actuals_df.loc[start_time:stop_time,:].copy().drop(columns=["TotalPower"])
        persistence_wind_fcst_df.index = persistence_wind_fcst_df.index + pd.Timedelta(minutes=5.0)
        # display(persistence_wind_fcst_df)
        # There are some dummy/unused variables currently being used in the funciton
        # calls that we will set to None.
        pv_fcst_df = None
        prev_set_points = None
        n_periods = 1
        step = 5.0
        (base_cost, set_points) = self.ego.base_cost(start_time,
                                           pv_fcst_df, # Currently unused
                                           persistence_wind_fcst_df,
                                           prev_set_points, # Currently unused
                                           n_periods, # Currently unused
                                           step, # Currently unused
                                           self.opflow_options_dict,
                                           system="Mac"
                                           )

        # Check if base cost is the same on all ranks
        self.ego.comm.Barrier()
        base_cost_arr = self.ego.comm.gather(base_cost, root=0)
        if my_mpi_rank == 0:
            base_cost_arr = np.asarray(base_cost_arr)
            assert abs(base_cost_arr - base_cost).all() < 1.e-6
        self.ego.comm.Barrier()

        # Turn deviations into scenarios
        wind_scen_df = binned_scenarios_df + persistence_wind_fcst_df.loc[:,binned_scenarios_df.columns].values
        for wgen in wind_scen_df.columns:
            # Enforce Pmax on wind scenarios
            wgen_max = self.ego.wind_max.loc[wgen]
            idx = wind_scen_df.loc[:,wgen] > wgen_max
            wind_scen_df.loc[idx,wgen] = wgen_max
            # Enforce Pmin on wind scenarios
            idx = wind_scen_df.loc[:,wgen] < 0.0
            wind_scen_df.loc[idx,wgen] = 0.0

        self.ego.comm.Barrier()
        if my_mpi_rank == 0:
            print('Available Scenarios = ', wind_scen_df.shape[0], ", Requested Scenarios = ", int(self.sopflow_options_dict['nscenarios']))

        assert wind_scen_df.shape[0] >= int(self.sopflow_options_dict['nscenarios'])
        nscen_global = int(self.sopflow_options_dict['nscenarios']) # wind_scen_df.shape[0]

        nscen_local_arr = np.zeros(comm_size, dtype=int)
        quotient, remainder = divmod(nscen_global, comm_size)
        nscen_local_arr[:] = quotient
        nscen_local_arr[0:remainder] += 1 # Divide the remainder evenly across the first n_remainder ranks
        # Sanity Check of MPI
        assert nscen_local_arr.sum() == nscen_global, "Scenarios were not properly divided across the MPI ranks"

        # Now get the starting and stopping location of a given rank
        if my_mpi_rank == 0:
            idx_offset = 0
        else:
            idx_offset = nscen_local_arr[0:my_mpi_rank].sum() # np.dot(np.arange(my_mpi_rank)+1, nscen_local_arr[0:my_mpi_rank])

        q_cost_local = np.zeros(nscen_local_arr[my_mpi_rank])
        local_opf_scen_dict = {}
        # time_offset = idx_offset*step
        # ts = wind_scen_df.index[0] + pd.Timedelta(minutes=time_offset)
        for i in range(nscen_local_arr[my_mpi_rank]):
            ts = wind_scen_df.index[i] # Will not work for MPI
            local_opf_scen_dict[i] = OPFLOW()
            opf_object = local_opf_scen_dict[i] # For convenience as of now
            opf_object.read_mat_power_data(self.ego.network_file)
            opf_object.setup_ps()

            # Constrain the thermal generation for the second stage
            self.ego._fix_non_wind(opf_object, set_points)

            # The following two lines are copied directly from base_cost for now.
            # Revisit them again
            wf_df = wind_scen_df.loc[ts:ts, :]
            self.ego._set_wind_new(opf_object, wf_df, self.ego.imat.get_table('gen'), self.ego.gen_type)

            # Run the opflow realization
            opf_object.solve()
            opf_object.solution_to_ps()

            q_cost_local[i] = opf_object.objective_function

            # Increment the time-stamp so as to price other scenarios
            # ts += pd.Timedelta(minutes=step)

        q_cost_local -= base_cost

        if my_mpi_rank == 0:
            q_cost_global = np.empty(nscen_global, dtype=float)
        else:
            q_cost_global = None

        self.ego.comm.Barrier()
        self.ego.comm.Gatherv(sendbuf=q_cost_local,
                          recvbuf=(q_cost_global, nscen_local_arr),
                          root=0)

        if my_mpi_rank == 0:
            # Zip the numpy array into the timeseries
            cost_n = pd.Series(index=wind_scen_df.index, data=q_cost_global)
            print("q_cost_global = ", repr(q_cost_global))
            print("q_cost_global max = ", np.amax(q_cost_global))
            print("q_cost_global min = ", np.amin(q_cost_global))

        return cost_n

###############################################

class MatpowerHandler:

    table_cols = {'bus' : ["bus_i",
                           "type",
                           "Pd",
                           "Qd",
                           "Gs",
                           "Bs",
                           "area",
                           "Vm",
                           "Va",
                           "baseKV",
                           "zone",
                           "Vmax",
                           "Vmin",
                           "lam_P",
                           "lam_Q",
                           "mu_Vmax",
                           "mu_Vmin",
                           ],
                  'gen' : ["bus",
                           "Pg",
                           "Qg",
                           "Qmax",
                           "Qmin",
                           "Vg",
                           "mBase",
                           "status",
                           "Pmax",
                           "Pmin",
                           "Pc1",
                           "Pc2",
                           "Qc1min",
                           "Qc1max",
                           "Qc2min",
                           "Qc2max",
                           "ramp_agc",
                           "ramp_10",
                           "ramp_30",
                           "ramp_q",
                           "apf",
                           "mu_Pmax",
                           "mu_Pmin",
                           "mu_Qmax",
                           "mu_Qmin",
                           ],
                  }

    def __init__(self,
                 network_file,
                 dynamic_tables=['bus', 'gen'],
                 static_tables=['branch', 'gencost', 'bus_name', 'gentype', 'genfuel']):

        t0 = time.time()

        self.network_file = network_file

        static_tables = static_tables
        (tables, order) = self._parse_file(network_file,
                                           dynamic_tables + static_tables)

        t1 = time.time()

        self.table_order = order
        (self.header, self.footer, self.dynamic, self.static
         ) = self._split_tables(tables, dynamic_tables, static_tables)
        self._str_to_df(self.dynamic)

        t2 = time.time()

        # print("Parse file: {:g}(s)\nOther: {:g}(s)".format(t1 - t0, t2 - t1))
        self.parse_time = t1 - t0
        self.other_time = t2 - t1

        return

    def _parse_file(self, network_file, tables):

        tables = set(tables + ['header', 'footer'])
        sections = {tab:'' for tab in tables}
        order = []

        table_start = re.compile('^mpc.(\w+) = [\[\{]\s')
        table_end = re.compile('^[\]\}];\s')
        with open(network_file, 'r') as netfile:
            current_table = 'header'
            reading_table = False
            line = netfile.readline()
            while line:
                if reading_table:
                    m = table_end.match(line)
                else:
                    m = table_start.match(line)

                if m:
                    if reading_table:
                        # We have found the end of a table
                        reading_table = False
                        # Add end of table to the string
                        sections[current_table] += line
                        # Assume we will have no more tables until we find one
                        current_table = 'footer'

                    else:
                        # We have found the start of a table
                        reading_table = True
                        # Get which table we're reading
                        current_table = m.group(1)
                        # Add start of table
                        sections[current_table] = line
                        order.append(current_table)
                        # Clear the footer
                        sections['footer'] = ''
                else:
                    # Just the next line in the current table
                    sections[current_table] += line

                line = netfile.readline()

        return (sections, order)

    def _split_tables(self, tables, dynamic, static):
        dynamic_tables={}
        static_tables={}
        for key in tables.keys():
            if key == 'header':
                header = tables[key]
            elif key == 'footer':
                footer = tables[key]
            elif key in dynamic:
                dynamic_tables[key] = tables[key]
            else:
                static_tables[key] = tables[key]
                # if key not in static:
                #     print('Unspecified table "{}" is assumed to be static'.format(key))
        return (header, footer, dynamic_tables, static_tables)

    def _str_to_df(self, dynamic):
        for key in dynamic.keys():
            tab_str = dynamic[key]
            rows = []
            col_names = MatpowerHandler.table_cols[key]
            for line in tab_str.strip().split(sep='\n')[1:-1]:
                vals = line.strip(' \n\r\t;').split(sep='\t')
                rows.append(dict(zip(col_names, vals)))
            df = pd.DataFrame(rows, columns=col_names)
            for col in df.columns:
                df[col] = pd.to_numeric(df[col])
            dynamic[key] = df
        return

    def _write_dynamic_table(self, ostream, table_name, table):
        ostream.write('mpc.{} = [\n'.format(table_name))
        table.to_csv(ostream, sep='\t', index=False, header=False)
        ostream.write('];\n\n')
        return

    def _write_static_table(self, ostream, table_name, table):
        ostream.write(table)
        ostream.write('\n')
        return

    def get_table(self, table_name):
        if table_name in self.dynamic.keys():
            df = self.dynamic[table_name]
        else:
            raise KeyError("Unknown or static table: {}".format(table_name))
        return df

    def write_matpower_file(self, new_file_name):
        with open(new_file_name, 'w') as mp_file:
            self._write_static_table(mp_file, 'header', self.header)
            for tab in self.table_order:
                if tab in self.dynamic.keys():
                    self._write_dynamic_table(mp_file, tab, self.dynamic[tab])
                elif tab in self.static.keys():
                    self._write_static_table(mp_file, tab, self.static[tab])
                else:
                    print('Table {} neither dynamic nor static. Unable to print.'.format(tab))
            self._write_static_table(mp_file, 'footer', self.footer)
        return


class ExaGO:

    def __init__(self,
                 network_file,
                 load_dir,
                 grid_name,
                 real_load_file=None,
                 reactive_load_file=None,
                 year=2020):

        start_init = time.time()

        # MPI specifics
        self.comm = MPI.COMM_WORLD
        my_mpi_rank = self.comm.Get_rank()

        # self.exe_path = exe_path
        self.grid_name = grid_name
        self.network_file = network_file
        self.exago_ignore = -1.e6

        start = time.time()
        self.imat = MatpowerHandler(network_file)
        stop = time.time()
        # print("Read Matpower: {:g}(s)".format(stop - start))
        self.gen_df_org = self.imat.get_table('gen').copy()
        self.bus_df_org = self.imat.get_table('bus').copy()
        self.gids = self._assign_gen_ids(self.gen_df_org)

        # print("Reading in load data...")
        # Read in the load dataframes
        start = time.time()
        if real_load_file is None:
            raise ValueError("The real load file has not been specified.")
        else:
            p_df = pd.read_csv(real_load_file, index_col=0, parse_dates=True)
            p_df.index = p_df.index.map(lambda t: t.replace(year=year))
            self.p_load_df = p_df

        if reactive_load_file is None:
            raise ValueError("The reactive load file has not been specified.")
        else:
            q_df = pd.read_csv(reactive_load_file, index_col=0, parse_dates=True)
            q_df.index = q_df.index.map(lambda t: t.replace(year=year))
            self.q_load_df = q_df
        stop = time.time()
        # print("Done. Time: {:g}(s)".format(stop - start))

        # gen_type = self.imat.static['gentype'].split('\n')[1:-2]
        # assert len(gen_type) == self.gen_df_org.shape[0]
        # self.gen_type = pd.Series(map(lambda s: s.strip(' \t\r\n\';)'), gen_type))

        gen_type = self.imat.static['genfuel'].split('\n')[1:-2]
        # print("len(gen_type) = ", len(gen_type))
        # print("self.gen_df_org.shape[0] = ", self.gen_df_org.shape[0])
        assert len(gen_type) == self.gen_df_org.shape[0]
        self.gen_type = pd.Series(map(lambda s: s.strip(' \t\r\n\';)'), gen_type))

        idx = self._wind_gens(self.gen_type)

        buses = self.gen_df_org.loc[idx,'bus']
        gen_id = pd.Series(data=1, index=buses.index)
        for bus in set(buses):
            bidx = buses == bus
            gen_id.loc[bidx] = range(1,sum(bidx)+1)

        pmax = self.gen_df_org.loc[idx,'Pmax']
        pmax.index = (buses.apply(str)
                      + '_Wind_'
                      + gen_id.apply(str))
        self.wind_max = pmax

        # Recover ExaGO executables, we will check if the exist in PATH and
        # EXAGO_INSTALL_DIR
        self.opflow_executable = self._check_for_exago('opflow')
        self.sopflow_executable = self._check_for_exago('sopflow')
        print("opflow executable = ", self.opflow_executable)
        print("sopflow executable = ", self.sopflow_executable)

        stop_init = time.time()
        # print("Init complete. Time: {:g}(s)".format(stop_init - start_init))

        return

    def _check_for_exago(self, executable_name):
        # This function checks if an exago executable exists
        # Step 1: Check for exago executable in PATH
        val = 0
        for path in os.environ["PATH"].split(os.pathsep):
            exe_file = os.path.join(path, executable_name)
            if os.path.isfile(exe_file) and os.access(exe_file, os.X_OK):
                executable_full_path = path + '/' + executable_name
                val += 1
                print("ExaGO executable {0} found in PATH".format(executable_name))
                return executable_full_path

        assert val == 0
        print("ExaGO executables not found in PATH, checking in EXAGO_INSTALL_DIR")
        if "EXAGO_INSTALL_DIR" in os.environ:
            exe_file = os.path.join(os.environ["EXAGO_INSTALL_DIR"], 'sopflow')
            if os.path.isfile(exe_file) and os.access(exe_file, os.X_OK):
                executable_full_path = os.environ["EXAGO_INSTALL_DIR"] + '/' + executable_name
                val += 1
                print("ExaGO executable {0} found in EXAGO_INSTALL_DIR".format(executable_name))
                return executable_full_path
        else:
            raise ValueError("ExaGO executables not found either in $PATH or $EXAGO_INSTALL_DIR. Please use the former to point to the executables")


    def _non_wind_gens(self,
                       gen_type):
        return np.logical_not(self._wind_gens(gen_type))

    def _wind_gens(self,
                   gen_type):
        return (gen_type == 'wind')


    def _restore_org_gen_table(self):
        gen_df = self.imat.get_table('gen')
        for col in ['Pmax', 'Pmin', 'Pg', 'Qmax', 'Qmin', 'Qg']:
            gen_df.loc[:,col] = self.gen_df_org.loc[:,col]
        return

    def _set_load(self,
                  p_df,
                  q_df,
                  bus_df):
        # Bus loads not in the given dataframes are assumed to be zero
        bus_df.loc[:,'Pd'] = 0.0
        for bus in pd.to_numeric(p_df.columns):
            idx = (bus_df.loc[:,'bus_i'] == bus)
            bus_df.loc[idx,'Pd'] = p_df.loc[p_df.index[0],str(bus)]
        bus_df.loc[:,'Qd'] = 0.0
        for bus in pd.to_numeric(q_df.columns):
            idx = (bus_df.loc[:,'bus_i'] == bus)
            bus_df.loc[idx,'Qd'] = q_df.loc[q_df.index[0],str(bus)]
        return

    def _scale_load(self, bus_df, scaling_factor):
        bus_df.loc[:,'Pd'] = scaling_factor*bus_df.loc[:,'Pd']
        bus_df.loc[:,'Qd'] = scaling_factor*bus_df.loc[:,'Qd']
        return

    def _assign_gen_ids(self, gen_df):
        gbuses = gen_df.loc[:,'bus']
        gids = ['1 '] * gbuses.size
        if gbuses.size > gbuses.unique().size:
            for (k,bus) in enumerate(gbuses.unique()):
                idx = gen_df.loc[:,'bus'] == bus
                if idx.sum() > 1:
                    for (gid,gbus) in enumerate(gen_df.loc[idx,'bus']):
                        print("Giving generator at bus {} id {}".format(gbus, gid+1))
                        assert bus == gbus
                        gids[k] = "{:<2d}".format(gid)
                else:
                    pass
        elif gbuses.size < gbuses.unique().size:
            assert False
        else:
            pass
        return gids

    def _set_wind_new(self,
                      opf,
                      w_df,
                      gen_df,
                      gen_type
                      ):

        idx = self._wind_gens(gen_type)
        assert w_df.columns.size == idx.sum()

        for wgen in w_df.columns:
            bus = int(wgen.split('_')[0])
            gen_id = int(wgen.split('_')[2]) # - 1
            widx = (gen_df.loc[:,'bus'] == bus) & (idx)
            # print("wgen = ", wgen, "bus = ", repr(bus), "gen_id = ", type(gen_id))
            # print("widx = ", widx)
            if widx.sum() >= 1:
                if gen_id < 10:
                    g_id = str(gen_id) + " "
                else:
                    g_id = str(gen_id)
                opf.ps_set_gen_power_limits(bus, g_id, w_df.loc[w_df.index[0], wgen], 0, self.exago_ignore, self.exago_ignore)
            else:
                print("Unable to identify row corresponding to generator {}".format(wgen))

        # self.opf.ps_set_gen_power_limits(3, "1 ", 65, 0, exago_ignore, exago_ignore)
        return

    def _fix_non_wind(self, opflow_object, set_points):
        idx = self._non_wind_gens(self.gen_type) # Bool array of non-wind set points
        og_gen_df = self.imat.get_table('gen')   # Original generatar table from Matpower file
        pg_arr = set_points[0] # Real set-points
        qg_arr = set_points[1] # Reactive set points
        for i in range(og_gen_df.shape[0]):
            if idx[i]:
                bus = int(og_gen_df.loc[i, "bus"])
                opflow_object.ps_set_gen_power_limits(bus, self.gids[i],
                                                      pg_arr[i], pg_arr[i],
                                                      qg_arr[i], qg_arr[i])
        return


    def _extract_set_points(self, opf):
        idx = self._non_wind_gens(self.gen_type) # Bool array of non-wind set points
        og_gen_df = self.imat.get_table('gen')   # Original generatar table from Matpower file
        pg_set = np.zeros(og_gen_df.shape[0]) # real set points array
        qg_set = np.zeros(og_gen_df.shape[0]) # reactive set points array

        for i in range(og_gen_df.shape[0]):
            if idx[i]:
                # We will only collect the set points of the non-wind_generators
                # print("i = ", i, ", self.gids[i] = ", repr(self.gids[i]))
                pg_set[i], qg_set[i] = opf.get_gen_dispatch(int(og_gen_df.loc[i, "bus"]), self.gids[i])
                # print("bus ", int(og_gen_df.loc[i, "bus"]), " pg = ", pg_set[i], " qg = " , qg_set[i])

        return (pg_set, qg_set)


    def _cleanup(self):
        if path.exists("opflowout.m"):
            os.remove("opflowout.m")
        if path.exists("sopflowout"):
            shutil.rmtree("sopflowout")
        if path.exists("case_{0}.m".format(self.grid_name)):
            os.remove("case_{0}.m".format(self.grid_name))

    def base_cost(self,
                  start_time,
                  pv_fcst_df, # Currently unused
                  wind_fcst_df,
                  prev_set_points, # Currently unused
                  n_periods, # Currently unused
                  step, # Currently unused
                  opflow_options_dict,
                  system="Summit"
                  ):

        t0 = time.time()

        stop_time = start_time
        self._restore_org_gen_table()

        # Create OPFLOW object
        self.opf_base = OPFLOW()
        self.opf_base.read_mat_power_data(self.network_file)
        self.opf_base.setup_ps()

        p_df = self.p_load_df.loc[start_time:stop_time, :]
        q_df = self.q_load_df.loc[start_time:stop_time, :]
        # self._set_load(p_df, q_df, self.imat.get_table('bus')) # Uncomment for our load
        # self._scale_load(self.imat.get_table('bus'), 0.9)

        wf_df = wind_fcst_df
        self._set_wind_new(self.opf_base, wf_df, self.imat.get_table('gen'), self.gen_type)

        t1 = time.time()

        # matpower_file = os.path.join('case_{}.m'.format(self.grid_name))
        # self.imat.write_matpower_file(matpower_file)

        t2 = time.time()

        # Python call
        self.opf_base.solve()
        self.opf_base.solution_to_ps()

        t3 = time.time()
        obj = self.opf_base.objective_function
        set_points = self._extract_set_points(self.opf_base)
        # idx = self._non_wind_gens(self.gen_type)
        # set_points = result.get_table('gen').loc[idx,:]

        t4 = time.time()



        t5 = time.time()

        elapsed = t5 - t0
        print("""**** Base Cost Timing ****
Change Tables: {:g}(s)  {:g}(%)
Write Tables: {:g}(s)  {:g}(%)
ExaGO: {:g}(s)  {:g}(%)
Set Points: {:g}(s)  {:g}(%)
Base Cost: {:g}(s)  {:g}(%)
Total: {:g}(s)
""".format(
    t1 - t0, (t1 - t0)/elapsed * 100,
    t2 - t1, (t2 - t1)/elapsed * 100,
    t3 - t2, (t3 - t2)/elapsed * 100,
    t4 - t3, (t4 - t3)/elapsed * 100,
    t5 - t4, (t5 - t4)/elapsed * 100,
    elapsed
),
              flush=True
              )

        return (obj, set_points)


    def cost_scenarios(self,
                           start_time,
                           pv_fcst_df, # Currently unused
                           wind_fcst_df,
                           wind_dev_df,
                           prev_set_points, # Currently unused
                           opflow_options_dict,
                           sopflow_options_dict,
                           n_periods=1, # Currently unused
                           step=5.0, # Currently unused
                           system="Summit"
                           ):

        my_mpi_rank = self.comm.Get_rank()
        comm_size = self.comm.Get_size()
        if my_mpi_rank == 0:
            # self._cleanup() # We may not need it so candidate for deletion
            pass

        # Lets run the base cost simulation on every rank and scenario binning
        (base_cost, set_points) = self.base_cost_lib(start_time,
                                           pv_fcst_df, # Currently unused
                                           wind_fcst_df,
                                           prev_set_points, # Currently unused
                                           n_periods, # Currently unused
                                           step, # Currently unused
                                           opflow_options_dict,
                                           system=system
                                           )
        # Check if base cost is the same on all ranks
        self.comm.Barrier()
        base_cost_arr = self.comm.gather(base_cost, root=0)
        if my_mpi_rank == 0:
            base_cost_arr = np.asarray(base_cost_arr)
            assert abs(base_cost_arr - base_cost).all() < 1.e-6
        self.comm.Barrier()

        # Turn deviations into scenarios
        stop_time = start_time
        w_scen_df = wind_fcst_df.loc[start_time:stop_time,:]
        wind_scen_df = wind_dev_df + w_scen_df.loc[:,wind_dev_df.columns].values
        for wgen in wind_scen_df.columns:
            # Enforce Pmax on wind scenarios
            wgen_max = self.wind_max.loc[wgen]
            idx = wind_scen_df.loc[:,wgen] > wgen_max
            wind_scen_df.loc[idx,wgen] = wgen_max
            # Enforce Pmin on wind scenarios
            idx = wind_scen_df.loc[:,wgen] < 0.0
            wind_scen_df.loc[idx,wgen] = 0.0

        self.comm.Barrier()
        if my_mpi_rank == 0:
            print('Available Scenarios = ', wind_scen_df.shape[0], ", Requested Scenarios = ", int(sopflow_options_dict['nscenarios']))

        assert wind_scen_df.shape[0] >= int(sopflow_options_dict['nscenarios'])
        nscen_global = int(sopflow_options_dict['nscenarios']) # Total number of scenarios that need to be priced

        nscen_local_arr = np.zeros(comm_size, dtype=int)
        quotient, remainder = divmod(nscen_global, comm_size)
        nscen_local_arr[:] = quotient
        nscen_local_arr[0:remainder] += 1 # Divide the remainder evenly across the first n_remainder ranks
        # Sanity Check of MPI
        assert nscen_local_arr.sum() == nscen_global, "Scenarios were not properly divided across the MPI ranks"

        # Now get the starting and stopping location of a given rank
        if my_mpi_rank == 0:
            idx_offset = 0
        else:
            idx_offset = nscen_local_arr[0:my_mpi_rank].sum() # np.dot(np.arange(my_mpi_rank)+1, nscen_local_arr[0:my_mpi_rank])

        q_cost_local = np.zeros(nscen_local_arr[my_mpi_rank])
        local_opf_scen_dict = {}
        time_offset = idx_offset*step
        ts = wind_scen_df.index[0] + pd.Timedelta(minutes=time_offset)
        for i in range(nscen_local_arr[my_mpi_rank]):
            local_opf_scen_dict[i] = OPFLOW()
            opf_object = local_opf_scen_dict[i] # For convenience as of now
            opf_object.read_mat_power_data(self.network_file)
            opf_object.setup_ps()

            # Constrain the thermal generation for the second stage
            self._fix_non_wind(opf_object, set_points)

            # The following two lines are copied directly from base_cost for now.
            # Revisit them again
            wf_df = wind_scen_df.loc[ts:ts, :]
            self._set_wind_new(opf_object, wf_df, self.imat.get_table('gen'), self.gen_type)

            # Run the opflow realization
            opf_object.solve()
            opf_object.solution_to_ps()

            q_cost_local[i] = opf_object.objective_function

            # Increment the time-stamp so as to price other scenarios
            ts += pd.Timedelta(minutes=step)

        q_cost_local -= base_cost

        if my_mpi_rank == 0:
            q_cost_global = np.empty(nscen_global, dtype=float)
        else:
            q_cost_global = None
        self.comm.Barrier()

        self.comm.Gatherv(sendbuf=q_cost_local,
                          recvbuf=(q_cost_global, nscen_local_arr),
                          root=0)

        self.comm.Barrier()

        return q_cost_global