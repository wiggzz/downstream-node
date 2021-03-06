from flask import request, g, render_template
from line_profiler import LineProfiler
import inspect

import linecache

from .startup import app

from . import node, routes, utils


def get_module_function_info(module):
    functions = list()
    for item in inspect.getmembers(module, inspect.isfunction):
        # print('Inspecting member {0}'.format(item))
        try:
            functions.append(item[1])
        except:
            pass

    return functions


def collect_module_functions(modules):
    functions = list()
    for m in modules:
        # print('Inspecting {0}'.format(m))
        functions.extend(get_module_function_info(m))
    return functions


@app.before_request
def start_profiling():
    if (app.config['PROFILE'] and app.mongo_logger is not None):
        if (not hasattr(g, 'profiler') or g.profiler is None):
            setattr(g, 'profiler', LineProfiler())
            # print('Collecting function info')
            function_info = collect_module_functions([node, routes, utils])
            for f in function_info:
                # print('Adding profile framework for function {0}'.format(f))
                g.profiler.add_function(f)
        g.profiler.enable()


def timing_key_to_str(k):
    return '_'.join([k[0], str(k[1]), k[2]]).replace('.', '_')


@app.teardown_request
def finish_profiling(exception=None):
    if (app.config['PROFILE'] and app.mongo_logger is not None):
        g.profiler.disable()
        stats = g.profiler.get_stats()
        # stats is an object with these properties:
        # timings : dict
        #   Mapping from (filename, first_lineno, function_name) of the
        #   profiled
        #   function to a list of (lineno, nhits, total_time) tuples for each
        #   profiled line. total_time is an integer in the native units of the
        #   timer.
        # unit : float
        #   The number of seconds per timer unit.
        functions = list(stats.timings.keys())
        lines = list(stats.timings.values())

        app.mongo_logger.db.profiling.insert(
            {'path': request.path,
             'functions': functions,
             'lines': lines,
             'unit': stats.unit})


def get_function_source_hits(logged_function, line_hits, unit):
    filename = logged_function[0]
    source_hits = list()
    for lineno in range(logged_function[1], min([l[0] for l in line_hits])):
        function_def = linecache.getline(filename, lineno)
        source_hits.append((function_def.rstrip(), None, None))
    for hit in line_hits:
        source_line = linecache.getline(filename, hit[0])
        source_hits.append(
            (source_line.rstrip(), hit[1], float(hit[2]) * float(unit)))
    return source_hits


@app.route('/profile/<path:path>')
def profiling_profile(path):
    if (app.config['PROFILE'] and app.mongo_logger is not None):
        mod_path = '/' + path
        requests = list()
        print('Showing profile for route: {0}'.format(mod_path))
        for p in app.mongo_logger.db.profiling.find({'path': mod_path}):
            request = dict(path=p['path'],
                           functions=list())
            for i in range(0, len(p['functions'])):
                if any([len(l) > 0 for l in p['lines'][i]]):
                    function = dict(
                        name=p['functions'][i][2],
                        filename=p['functions'][i][0],
                        lines=get_function_source_hits(p['functions'][i],
                                                       p['lines'][i],
                                                       p['unit']))
                    request['functions'].append(function)
            requests.append(request)

        return render_template('profile.html', path=path, requests=requests)
    else:
        return 'Profiling disabled.  Sorry!'
