#!/usr/bin/env python
__author__ = "Gao Wang"
__copyright__ = "Copyright 2016, Stephens lab"
__email__ = "gaow@uchicago.edu"
__license__ = "MIT"
import os, yaml, json, glob
from collections import OrderedDict
import pandas as pd
from pysos.utils import Error
from .utils import load_rds, save_rds, sos_pair_input, ordered_load, \
     flatten_list
import readline
import rpy2.robjects.vectors as RV

class ResultDBError(Error):
    """Raised when there is a problem building the database."""
    def __init__(self, msg):
        Error.__init__(self, msg)
        self.args = (msg, )

class ResultDB:
    def __init__(self, db_name):
        self.name = db_name
        # different tables; one exec per table
        self.data = {}
        # master tables
        self.master = {}
        # list of exec names that are the last step in sequence
        self.last_block = []
        # key = block name, item = exec name
        self.groups = {}

    def load_parameters(self):
        def search_dependent_index(x):
            res = None
            for ii, kk in enumerate(data.keys()):
                if kk.split('_')[1] == x:
                    res = ii + 1
                    break
            if res is None:
                raise ResultDBError('Cannot find dependency step for output ``{}``!'.format(x))
            return res
        #
        try:
            data = OrderedDict()
            for item in glob.glob('.sos/.dsc/*{}.yaml.tmp'.format(os.path.basename(self.name))):
                with open(item) as f: data.update(ordered_load(f, yaml.SafeLoader))
        except FileNotFoundError:
            raise ResultDBError('Cannot load source data to build database!')
        seen = []
        for k in list(data.keys()):
            k1 = '_'.join(k.split('_')[1:])
            if not k1 in seen:
                seen.append(k1)
            else:
                del data[k]
        for idx, (k, v) in enumerate(data.items()):
            # each v is a dict
            # collect some meta info
            table = v['exec']
            block_name = v['step_name'].split('_')[0]
            if block_name not in self.groups:
                self.groups[block_name] = []
            if table not in self.groups[block_name]:
                self.groups[block_name].append(table)
            if not block_name in self.last_block and v['step_name'] == v['sequence_name'].split('+')[-1]:
                self.last_block.append(block_name)
            #
            for x in ['step_id', 'return', 'depends']:
                if x in v.keys():
                    v['.{}'.format(x)] = v.pop(x)
            #
            if not table in self.data:
                self.data[table] = {}
                for x in list(v.keys()) + ['step_id', 'return', 'depends']:
                    if x not in ['sequence_id', 'sequence_name', 'step_name', 'exec']:
                        self.data[table][x] = []
            else:
                keys1 = repr(sorted([x for x in v.keys() if not x in
                                     ['sequence_id', 'sequence_name', 'step_name', 'exec']]))
                keys2 = repr(sorted([x for x in self.data[table].keys() if not x in
                                     ['step_id', 'return', 'depends']]))
                if keys1 != keys2:
                    raise ResultDBError('Inconsistent keys between step '\
                                              '``{1} (value {3})`` and ``{2} (value {4})``.'.\
                                              format(idx + 1, keys1, self.data[table]['step_id'], keys2))
            self.data[table]['step_id'].append(idx + 1)
            k = k.split('_')
            self.data[table]['return'].append(k[1])
            if len(k) > 2:
                self.data[table]['depends'].append(search_dependent_index(k[-1]))
            else:
                self.data[table]['depends'].append(None)
            for k1, v1 in v.items():
                if k1 not in ['sequence_id', 'sequence_name', 'step_name', 'exec']:
                    self.data[table][k1].append(v1)

    def __find_block(self, step):
        for k in self.groups:
            if step in self.groups[k]:
                return k
        raise ResultDBError('Cannot find ``{}`` in any blocks!'.format(step))

    def __get_sequence(self, step, step_id, step_idx, res):
        '''Input are last step name, ID, and corresponding index (in its data frame)'''
        res.append((step, step_id))
        depend_id = self.data[step]['depends'][step_idx]
        if depend_id is None:
            return
        else:
            idx = None
            step = None
            for k in self.data:
                # try get some idx
                if depend_id in self.data[k]['step_id']:
                    idx = self.data[k]['step_id'].index(depend_id)
                    step = k
                    break
            if idx is None or step is None:
                raise ResultDBError('Cannot find step_id ``{}`` in any tables!'.format(depend_id))
            self.__get_sequence(step, depend_id, idx, res)


    def write_master_table(self, block):
        '''
        Create a master table in DSCR flavor. Columns are:
        name, block1, block1_ID, block2, block2_ID, ...
        I'll create multiple master tables for as many as last steps.
        Also extend the master table to include information from the
        output of last step
        (step -> id -> depend_id -> step ... )_n
        '''
        res = []
        for step in self.groups[block]:
            for step_idx, step_id in enumerate(self.data[step]['step_id']):
                tmp = []
                self.__get_sequence(step, step_id, step_idx, tmp)
                tmp.reverse()
                res.append(tmp)
        data = {}
        for item in res:
            key = tuple([self.__find_block(x[0]) for x in item])
            if key not in data:
                data[key] = [flatten_list([('{}_name'.format(x), '{}_id'.format(x)) for x in key])]
            data[key].append(flatten_list(item))
        for key in data:
            header = data[key].pop(0)
            data[key] = pd.DataFrame(data[key], columns = header)
        return pd.concat([data[key] for key in data], axis = 1)


    def __load_output(self):
        for k, v in self.data.items():
            for item in v['__output__']:
                rds = '{}/{}.rds'.format(self.name, item)
                if not os.path.isfile(rds):
                    continue
                rdata = load_rds(rds, types = (RV.Array, RV.IntVector, RV.FloatVector, RV.StrVector,
                                               RV.DataFrame))
                for k1, v1 in rdata.items():
                    # a "simple" object
                    if len(v1) == 1 and '{}__{}'.format(k1, v['exec']) not in v:
                        self.data[k]['{}__{}'.format(k1, v['exec'])] = v1[0]


    def cbind_output(self, name, table):
        '''
        For output from the last step of sequence (the output we ultimately care),
        if output.rds exists, then try to read that RDS file and
        dump its values to parameter space if it is "simple" enough (i.e., 1D vector at the most)
        load those values to the master table directly. Keep the parameters
        of those steps separate.
        '''
        data = []
        colnames = None
        for step, idx in zip(table['{}_name'.format(name)], table['{}_id'.format(name)]):
            rds = '{}/{}.rds'.format(self.name,
                                     self.data[step]['return'][self.data[step]['step_id'].index(idx)])
            if not os.path.isfile(rds):
                continue
            rdata = load_rds(rds, types = (RV.Array, RV.IntVector, RV.FloatVector, RV.StrVector))
            tmp_colnames = []
            values = []
            for k in sorted(rdata.keys()):
                if len(rdata[k]) == 1:
                    tmp_colnames.append(k)
                    values.append(rdata[k][0])
                else:
                    tmp_colnames.extend(['{}_{}'.format(k, idx + 1) for idx in range(len(rdata[k]))])
                    values.extend(rdata[k])
            if colnames is None:
                colnames = tmp_colnames
            else:
                if colnames != tmp_colnames:
                    raise ResultDBError('Variables in ``{}`` are not consistent with existing variables!'.\
                                        format(rds))
            data.append([idx] + values)
        # Now bind data to table, by '{}_id'.format(name)
        return pd.merge(table, pd.DataFrame(data, columns = ['{}_id'.format(name)] + colnames),
                        on = '{}_id'.format(name), how = 'outer')

    def Build(self):
        self.load_parameters()
        for block in self.last_block:
            self.master['master_{}'.format(block)] = self.write_master_table(block)
        for item in self.master:
            self.master[item] = self.cbind_output(item.split('_', 1)[1], self.master[item])
        tmp = ['step_id', 'depends', 'return']
        for table in self.data:
            cols = tmp + [x for x in self.data[table].keys() if x not in tmp]
            self.data[table] = pd.DataFrame(self.data[table], columns = cols)
        self.data.update(self.master)
        save_rds(self.data, self.name + '.rds')


class ConfigDB:
    def __init__(self, db_name):
        self.name = db_name

    def Build(self):
        ''''''
        self.data = {}
        for f in glob.glob('.sos/.dsc/*.io.tmp'):
            fid, sid, name = os.path.basename(f).split('.')[:3]
            if fid not in self.data:
                self.data[fid] = {}
            if sid not in self.data[fid]:
                self.data[fid][sid] = {}
            if name not in self.data[fid][sid]:
                self.data[fid][sid][name] = {}
            x, y, z= open(f).read().strip().split('::')
            self.data[fid][sid][name]['input'] = [os.path.join(self.name, os.path.basename(item))
                                                   for item in x.split(',') if item]
            self.data[fid][sid][name]['output'] = [os.path.join(self.name, os.path.basename(item))
                                                   for item in y.split(',') if item]
            if int(z) != 0:
                # FIXME: need a more efficient solution
                self.data[fid][sid][name]['input'] = sos_pair_input(self.data[fid][sid][name]['input'])
        #
        with open('.sos/.dsc/{}.conf'.format(os.path.basename(self.name)), 'w') as f:
            f.write(json.dumps(self.data))
