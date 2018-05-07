import math
import re
import json
import logging
import os
import os.path
import random
import datetime
import time

logger = logging.getLogger("track.elasticlogs")

available_dashboards = ['traffic', 'content_issues']

epoch = datetime.datetime.utcfromtimestamp(0)

class Error(Exception):
    """Base class for exceptions in this module."""
    pass

class ConfigurationError(Error):
    """Exception raised for parameter errors.

    Attributes:
        message -- explanation of the error
    """

    def __init__(self, message):
        self.message = message

class ElasticlogsKibanaSource:
    """
    Simulates a set of sample Kibana dashboards for the elasticlogs data set.

    It expects the parameter hash to contain the following keys:
        "dashboard"            -   String indicating which dashboard to simulate. Defaults to 'traffic'.
        "query_string"         -   String or list of strings indicating query parameters to randomize during benchmarking. Defaults to "*", If a 
                                   list has been specified, a random value will be selected.
        "index_pattern"        -   String opr list of strings representing the index pattern to query. Defaults to 'elasticlogs-*'. If a list has 
                                   been specified, a random value will be selected.    
        "fieldstats_id".       -   fieldstats_id to base relative window definitions on. (not mandatory)
        "window_end"           -   Specification of aggregation window end or period within which it should end. If one single value is specified, 
                                   that will be used to anchor the window. If two values are given in a comma separated list, the end of the window
                                   will be randomized within this interval. Values can be either absolute or relative:
                                       'now' - Always evaluated to the current timestamp. This is the default value.
                                       'now-1h' - Offset to the current timestamp. Consists of a number and either m (minutes), h (hours) or d (days).
                                       '2016-12-20 20:12:32' - Exact timestamp.
                                       'START' - If an fieldstats_id has been provided, 'START' can be used to reference the start of this interval.
                                       'END' - If an fieldstats_id has been provided, 'END' can be used to reference the end of this interval.
                                       'END-40%' - When an interval has been specified through an fieldstats_id, it is possible to express a volume
                                       relative to the size of the interval as a percentage. If we assume the interval covers 10 hours, 'END-40%'
                                       represents the timestamp 4 hours (40% of the 10 hour interval) before the END timestamp.
        "window_length"        -   String indicating length of the time window to aggregate across. Values can be either absolute 
                                   or relative. Defaults to '1d'.
                                       '4d' - Consists of a number and either m (minutes), h (hours) or d (days). Can not be lower than 1 minute.
                                       '10%' - Length given as percentage of window size. Only available when fieldstats_id have been specified.
    """
    def __init__(self, track, params, **kwargs):
        self._params = params
        self._indices = track.indices
        self._index_pattern = 'elasticlogs-*'
        self._query_string_list = ['*']
        self._dashboard = 'traffic'
        
        random.seed()

        if 'index_pattern' in params.keys():
            self._index_pattern = params['index_pattern']
        
        if 'query_string' in params.keys():
            self._query_string_list = params['query_string']

        if 'dashboard' in params.keys():
            if params['dashboard'] in available_dashboards:
                self._dashboard = params['dashboard']
            else:
                logger.info("[kibana] Illegal dashboard configured ({}). Using default dashboard instead.".format(params['dashboard']))

        if 'fieldstats_id' in params.keys():
            file_name = "{}/.rally/temp/{}.json".format(os.environ['HOME'], params['fieldstats_id'])
            if os.path.isfile(file_name):
                filedata = open(file_name, 'r').read()
                data = json.loads(filedata)
                self._fieldstats_start_ms = data['ts_min_ms']
                self._fieldstats_end_ms = data['ts_max_ms']
                self._fieldstats_provided = True
            else:
                raise ConfigurationError('fieldstats_id does not correspond to exiasting file.')
        else:
            self._fieldstats_provided = False

        # Validate window length(s)
        if 'window_length' in params.keys():
            self._window_length = params['window_length']
        else:
            self._window_length = '1d'

        re1 = re.compile("^(\d+\.*\d*)([dhm])$")
        re2 = re.compile("^(\d+\.*\d*)%$")

        m1 = re1.match(self._window_length)
        m2 = re2.match(self._window_length)
        if m1:
            val = float(m1.group(1))
            unit = m1.group(2)

            if unit == "m":
                self._window_duration_ms = int(60*val*1000)
            elif unit == "h":
                self._window_duration_ms = int(3600*val*1000)
            else:
                self._window_duration_ms = int(86400*val*1000)
        elif m2:
            if self._fieldstats_provided:
                val = int(math.fabs(float(m2.group(1)) / 100.0) * (self._fieldstats_end_ms - self._fieldstats_start_ms))
                self._window_duration_ms = val
            else:
                raise ConfigurationError('Invalid window_length as a percentage ({}) may only be used when fieldstats_id provided.'.format(wli))
        else:
            raise ConfigurationError('Invalid window_length parameter supplied: {}.'.format(wli))
                
        # Interpret window specification(s)
        if 'window_end' in params.keys():
            self._window_end = self.__parse_window_parameters(params['window_end'])
        else:
            self._window_end = [{'type': 'relative', 'offset_ms': 0}]

    def partition(self, partition_index, total_partitions):
        seed = partition_index * self._params["seed"] if "seed" in self._params else None
        random.seed(seed)
        return self

    def size(self):
        return 1

    def params(self):
        # Determine window_end boundaries
        if len(self._window_end) == 1:
            ts_max_ms = int(self.__window_boundary_to_ms(self._window_end[0]))
        else:
            t1 = self.__window_boundary_to_ms(self._window_end[0])
            t2 = self.__window_boundary_to_ms(self._window_end[1])
            offset = (int)(random.random() * math.fabs(t2 - t1))

            ts_max_ms = int(offset + min(t1, t2))
        
        ts_min_ms = int(ts_max_ms - self._window_duration_ms)

        window_size_seconds = int(self._window_duration_ms / 1000)

        meta_data = {}

        # Determine histogram interval
        interval = self.__determine_interval(window_size_seconds, 50, 100)
        meta_data['interval'] = interval

        index_pattern = self.__select_random_item(self._index_pattern)
        meta_data['index_pattern'] = index_pattern

        query_string = self.__select_random_item(self._query_string_list)
        meta_data['query_string'] = query_string

        meta_data['dashboard'] = self._dashboard

        meta_data['window_length'] = self._window_length

        if self._dashboard == 'traffic':
            response = {"body": self.__traffic_dashboard(index_pattern, query_string, interval, ts_min_ms, ts_max_ms)}
        elif self._dashboard == 'content_issues':
            response = {"body": self.__content_issues_dashboard(index_pattern, query_string, interval, ts_min_ms, ts_max_ms)}
        
        response['meta_data'] = meta_data

        return response

    def __select_random_item(self, values):
        if isinstance(values, list):
            idx = random.randint(0, len(values)-1)
            return values[idx] 
        else:
            return values

    def __window_boundary_to_ms(self, wb):
        if wb['type'] == 'relative':
            ct = self.__current_epoch_ms()
            cta = ct + wb['offset_ms']
            return cta
        else:
            return wb['ts_ms']
    
    def __current_epoch_ms(self):
        dt = datetime.datetime.utcnow()
        return (dt-epoch).total_seconds() *1000

    def __parse_window_parameters(self, window_spec):
        items = window_spec.split(',')
        window_end_spec = []

        re1 = re.compile("^now([+-]\d+\.*\d*)([dhm])$")
        re2 = re.compile("^\d{4}.\d{2}.\d{2}.\d{2}.\d{2}.\d{2}$")
        re3 = re.compile("^(START|END)$")
        re4 = re.compile("^(START|END)([+-]\d+\.*\d*)%$")

        for i in items:
            m1 = re1.match(i)
            m2 = re2.match(i)
            m3 = re3.match(i)
            m4 = re4.match(i)

            if i == "now":
                window_end_spec.append({'type': 'relative', 'offset_ms': 0})
            elif m1:
                val = float(m1.group(1))
                unit = m1.group(2)
                offset_ms = 0

                if unit == "m":
                    offset_ms = int(60*val*1000)
                elif unit == "h":
                    offset_ms = int(3600*val*1000)
                else:
                    offset_ms = int(86400*val*1000)

                window_end_spec.append({'type': 'relative', 'offset_ms': offset_ms})
            elif m2:
                c = re.split('\D+', i)

                dt = datetime.datetime.strptime('{} {} {} {} {} {} UTC'.format(c[0], c[1], c[2], c[3], c[4], c[5]), "%Y %m %d %H %M %S %Z")
                epoch_ms = (int)((dt-epoch).total_seconds() * 1000)
                window_end_spec.append({'type': 'absolute', 'ts_ms': epoch_ms})
            elif m3:
                if self._fieldstats_provided:
                    if m3.group(1) == 'START':
                        window_end_spec.append({'type': 'absolute', 'ts_ms': self._fieldstats_start_ms})
                    else:
                        window_end_spec.append({'type': 'absolute', 'ts_ms': self._fieldstats_end_ms})
                else:
                    raise ConfigurationError('Window end definition based on {} requires fieldstats_id has been specified.'.format(m3.group(1)))
            elif m4:
                if self._fieldstats_provided:
                    reference = m4.group(1)
                    percentage = float(m4.group(2)) / 100.0
                    
                    if reference == 'START':
                        epoch_ms = int(percentage * (self._fieldstats_end_ms - self._fieldstats_start_ms)) + self._fieldstats_start_ms
                    else:
                        epoch_ms = int(percentage * (self._fieldstats_end_ms - self._fieldstats_start_ms)) + self._fieldstats_end_ms
                    
                    window_end_spec.append({'type': 'absolute', 'ts_ms': epoch_ms})
                else:
                    raise ConfigurationError('fieldstats_id does not correspond to exiasting file.')

        return window_end_spec

    def __unit_string_to_milliseconds(self, string):
        m = re.match("^(\d+\.*\d*)([dhm])$", string)
        if m:
            val =  float(m.group(1))
            unit =  m.group(2)

            if unit == "m":
                return False, int(60*val*1000)
            elif unit == "h":
                return False, int(3600*val*1000)
            else:
                return False, int(86400*val*1000)
        else:
            return True, None

    def __determine_interval(self, window_size_seconds, target_bars, max_bars):
        available_intervals = [
            {'unit':'1s',      'length_sec': 1},
            {'unit':'5s',      'length_sec': 5},
            {'unit':'10s',     'length_sec': 10},
            {'unit':'30s',     'length_sec': 30},
            {'unit':'1m',      'length_sec': 60},
            {'unit':'5m',      'length_sec': 300},
            {'unit':'10m',     'length_sec': 600},
            {'unit':'30m',     'length_sec': 1800},
            {'unit':'1h',      'length_sec': 3600},
            {'unit':'3h',      'length_sec': 10800},
            {'unit':'12h',     'length_sec': 43200},
            {'unit':'1d',      'length_sec': 86400},
            {'unit':'week',    'length_sec': 604800},
            {'unit':'month',   'length_sec': 2592000},
            {'unit':'quarter', 'length_sec': 7776000},
            {'unit':'year',    'length_sec': 31536000}
        ]

        min_interval_size = window_size_seconds / max_bars
        target_interval_size = window_size_seconds / target_bars
        i1 = None
        i2 = None

        for interval in available_intervals:
            if interval['length_sec'] < target_interval_size:
                i1 = interval

            if interval['length_sec'] >= target_interval_size:
                i2 = interval
                break 

        if i1 is None:
          selected_interval = i2['unit']
        elif i2 is None:
          selected_interval = i1['unit']
        elif i1['length_sec'] > min_interval_size:
            selected_interval = i1['unit']
        else:
            selected_interval = i2['unit']

        return selected_interval

    def __get_preference(self):
        return int(round(time.time() * 1000))

    def __print_ts(self, ts):
        ts_s = int(ts / 1000)
        dt = datetime.datetime.utcfromtimestamp(ts_s)
        return dt.isoformat()

    def __content_issues_dashboard(self, index_pattern, query_string, interval, ts_min_ms, ts_max_ms):
        preference = self.__get_preference()

        return [
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"query":{"bool":{"must":[{"query_string":{"query":"*","analyze_wildcard":True}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}},"size":0,"aggs":{}},
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"size":0,"aggs":{"2":{"date_histogram":{"field":"@timestamp","interval":interval,"time_zone":'Europe/London',"min_doc_count":1}}},"highlight":{"pre_tags":["@kibana-highlighted-field@"],"post_tags":["@/kibana-highlighted-field@"],"fields":{"*":{}},"require_field_match":False,"fragment_size":2147483647},"query":{"bool":{"must":[{"query_string":{"query":"*","analyze_wildcard":True}},{"match":{"response":{"query":404,"type":"phrase"}}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}}},
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"size":0,"aggs":{"2":{"filters":{"filters":{"Internal referrals":{"query_string":{"query":"referrer: \"www.elastic.co\"","analyze_wildcard":True}},"External referrals":{"query_string":{"query":"-referrer: \"www.elastic.co\"","analyze_wildcard":True}}}}}},"highlight":{"pre_tags":["@kibana-highlighted-field@"],"post_tags":["@/kibana-highlighted-field@"],"fields":{"*":{}},"require_field_match":False,"fragment_size":2147483647},"query":{"bool":{"must":[{"query_string":{"query":"*","analyze_wildcard":True}},{"match":{"response":{"query":404,"type":"phrase"}}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}}},
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"size":0,"aggs":{"2":{"terms":{"field":"referrer.keyword","size":20,"order":{"_count":"desc"}}}},"highlight":{"pre_tags":["@kibana-highlighted-field@"],"post_tags":["@/kibana-highlighted-field@"],"fields":{"*":{}},"require_field_match":False,"fragment_size":2147483647},"query":{"bool":{"must":[{"query_string":{"query":"*","analyze_wildcard":True}},{"match":{"response":{"query":404,"type":"phrase"}}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}}},
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"size":0,"aggs":{"3":{"cardinality":{"field":"request.keyword"}}},"highlight":{"pre_tags":["@kibana-highlighted-field@"],"post_tags":["@/kibana-highlighted-field@"],"fields":{"*":{}},"require_field_match":False,"fragment_size":2147483647},"query":{"bool":{"must":[{"query_string":{"query":"*","analyze_wildcard":True}},{"match":{"response":{"query":404,"type":"phrase"}}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}}},
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"size":0,"aggs":{"2":{"terms":{"field":"request.keyword","size":20,"order":{"_count":"desc"}}}},"highlight":{"pre_tags":["@kibana-highlighted-field@"],"post_tags":["@/kibana-highlighted-field@"],"fields":{"*":{}},"require_field_match":False,"fragment_size":2147483647},"query":{"bool":{"must":[{"query_string":{"query":"*","analyze_wildcard":True}},{"match":{"response":{"query":404,"type":"phrase"}}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}}}
               ]

    def __traffic_dashboard(self, index_pattern, query_string, interval, ts_min_ms, ts_max_ms):
        preference = self.__get_preference()

        return [
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"query":{"bool":{"must":[{"query_string":{"query":"*","analyze_wildcard":True}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}},"size":0,"aggs":{}},
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"size":0,"aggs":{"2":{"date_histogram":{"field":"@timestamp","interval":interval,"time_zone":'Europe/London',"min_doc_count":1},"aggs":{"3":{"terms":{"field":"response","size":10,"order":{"_count":"desc"}}}}}},"query":{"bool":{"must":[{"query_string":{"analyze_wildcard":True,"query":"*"}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}},"highlight":{"pre_tags":["@kibana-highlighted-field@"],"post_tags":["@/kibana-highlighted-field@"],"fields":{"*":{}},"require_field_match":False,"fragment_size":2147483647}},
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"size":0,"aggs":{},"query":{"bool":{"must":[{"query_string":{"analyze_wildcard":True,"query":"*"}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}},"highlight":{"pre_tags":["@kibana-highlighted-field@"],"post_tags":["@/kibana-highlighted-field@"],"fields":{"*":{}},"require_field_match":False,"fragment_size":2147483647}},
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"size":0,"aggs":{"2":{"terms":{"field":"request.keyword","size":20,"order":{"_count":"desc"}}}},"query":{"bool":{"must":[{"query_string":{"analyze_wildcard":True,"query":"*"}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}},"highlight":{"pre_tags":["@kibana-highlighted-field@"],"post_tags":["@/kibana-highlighted-field@"],"fields":{"*":{}},"require_field_match":False,"fragment_size":2147483647}},
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"size":0,"aggs":{"2":{"terms":{"field":"geoip.country_name","size":10,"order":{"_count":"desc"}}}},"query":{"bool":{"must":[{"query_string":{"analyze_wildcard":True,"query":"*"}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}},"highlight":{"pre_tags":["@kibana-highlighted-field@"],"post_tags":["@/kibana-highlighted-field@"],"fields":{"*":{}},"require_field_match":False,"fragment_size":2147483647}},
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"size":0,"aggs":{"2":{"geohash_grid":{"field":"geoip.location","precision":2}}},"query":{"bool":{"must":[{"query_string":{"analyze_wildcard":True,"query":"*"}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}},"highlight":{"pre_tags":["@kibana-highlighted-field@"],"post_tags":["@/kibana-highlighted-field@"],"fields":{"*":{}},"require_field_match":False,"fragment_size":2147483647}},
                   {"index":index_pattern,"ignore_unavailable":True,"preference":preference},
                   {"size":0,"aggs":{"4":{"terms":{"field":"useragent.os_name","size":10,"order":{"_count":"desc"}}}},"query":{"bool":{"must":[{"query_string":{"analyze_wildcard":True,"query":"*"}},{"query_string":{"query":query_string,"analyze_wildcard":True}},{"range":{"@timestamp":{"gte":ts_min_ms,"lte":ts_max_ms,"format":"epoch_millis"}}}],"must_not":[]}},"highlight":{"pre_tags":["@kibana-highlighted-field@"],"post_tags":["@/kibana-highlighted-field@"],"fields":{"*":{}},"require_field_match":False,"fragment_size":2147483647}}
               ]
