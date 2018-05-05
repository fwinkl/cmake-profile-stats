#!/usr/bin/env python

from __future__ import print_function

import argparse
import collections
import os
import re
import shelve
import sys
import textwrap


_INDENT_STEP = 2


def _process_arguments():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description='''
Process cmake execution log produced with --trace/--trace-expand command line
options.
Note: In order to provide command's timestamps CMake should be patched with
either of the diffs provided alongside this script and compiled from source''',
        epilog=textwrap.dedent('''
Each trace line in a report has 6 distinct parts:
1) nesting level in square brackets;
2) path to original cmake script (maybe with dots in the middle and dots at
   the end if -w option was used);
3) line number of a traced line in the original cmake script in parentheses;
4) a line of code as it was traced by cmake;
5) cumulative execution time of a traced line in seconds in parentheses;
6) percentage of cumulative execution time of a traced line to whole execution
   time in parentheses.
In short format is as follows:
[nesting]file_path(line_number):  cmake_code (seconds)(percentage)

During script execution it can output to stderr lines which it does not
recognize as cmake trace lines. Normally such lines originate from cmake
script's messages and this script outputs those lines starting with
"Ignored: " string.'''))

    parser.add_argument(
        'trace', nargs='?', default=None,
        help='cmake trace log or stdin')
    parser.add_argument(
        '-f', '--shelve-file', default='cmake.traces',
        help='file for shelf container, which is used in subsequent script '
             'runs without recurring log processing {default: %(default)s}')
    parser.add_argument(
        '-t', '--threshold', default=0, type=float,
        help='do not report traces with relative time lower than the '
             'threshold, for example 0.01 corresponds to 1%% of the whole '
             'execution time {default: %(default)s}')
    parser.add_argument(
        '-d', '--depth', default=0, type=int,
        help='do not report traces with depth bigger than requested (depth=0 '
             'is ignored) {default: %(default)s}')
    parser.add_argument(
        '--ignore-nesting', action='store_true',
        help='ignore nesting level field in input cmake log')
    parser.add_argument(
        '-w', '--trace-info-width', default=None, type=int,
        help='fixed width in characters of a variable part of cmake trace '
             '(file name, line number, nesting) in generated report '
             '{default: %(default)s}')
    parser.add_argument(
        '-s', '--sort-traces', action='store_true',
        help='sort subcalls in a trace according to their timings '
             '{default: %(default)s}')
    parser.add_argument(
        '-r', '--report-only', action='store_true',
        help='do not collect stats, make a report from shelved stats instead '
             '{default: %(default)s}')
    parser.add_argument(
        '-1', '--one', action='store_true',
        help='report only the most expensive stack trace '
             '{default: %(default)s}')
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='enable verbose output {default: %(default)s}')

    return parser.parse_args()


class _CmakeTraceInfo(object):
    def __init__(self, cmake_file, cmake_line, cmake_code_line):
        self.cmake_file = cmake_file
        self.cmake_line = cmake_line
        self.cmake_code_line = cmake_code_line

    def to_string_adjusted(self, width, nesting):
        file_width = width - (len(self.cmake_line) + len(nesting))
        cmake_file_len = len(self.cmake_file)

        adjusted_file = self.cmake_file
        if file_width < cmake_file_len:
            assert file_width >= 5
            half_file_width = file_width / 2
            adjusted_file = '{}...{}'.format(
                adjusted_file[:half_file_width - 1],
                adjusted_file[cmake_file_len + 2 - half_file_width:])

        adjusted_file = adjusted_file.ljust(file_width, '.')

        return '[{}]{}({}):  {}'.format(nesting, adjusted_file,
                                        self.cmake_line, self.cmake_code_line)

    def to_string_plain(self, width, nesting):
        assert width is None
        return '[{}]{}({}):  {}'.format(nesting, self.cmake_file,
                                        self.cmake_line, self.cmake_code_line)


class _CmakeTrace(object):
    def __init__(self, duration, ti, parent):
        self.duration = duration
        self.trace_info = ti
        self.parent_trace = parent
        self.subtraces = []

        try:
            while True:
                parent.duration = parent.duration + duration
                parent = parent.parent_trace
        except AttributeError:
            assert parent is None


def _update_traces(traces, parent_trace, current_timeval,
                   previous_timeval, current_nesting, previous_nesting,
                   previous_trace_info):
    assert isinstance(previous_timeval, float)
    duration = current_timeval - previous_timeval
    if duration < 0:
        duration = 0

    # current_nesting == 0 is kind of an impossible value unless cmake log
    # does not contain call nesting information.
    if current_nesting == 0:
        def enumerate_frames(trace):
            trace = trace.subtraces[-1] if trace.subtraces else trace
            while True:
                if trace.trace_info is None:
                    yield (trace, None, None)
                else:
                    yield (trace,
                           trace.trace_info.cmake_file,
                           trace.trace_info.cmake_line)

                if trace.parent_trace is None:
                    break

                trace = trace.parent_trace

        # If cmake log doesn't provide nesting info we try recreate this info
        # using a heuristics that ...
        insertion_trace = (sys.maxint, None)
        for (trace, cmake_file, cmake_line) in enumerate_frames(parent_trace):
            if cmake_file == previous_trace_info.cmake_file:
                distance = abs(cmake_line - previous_trace_info.cmake_line)
                if distance < insertion_trace[0]:
                    insertion_trace = (distance, trace.parent_trace)
            elif insertion_trace[1] is None:
                insertion_trace = (sys.maxint, trace)
        else:
            if insertion_trace[1] is not None:
                parent_trace = insertion_trace[1]

            parent_trace.subtraces.append(
                _CmakeTrace(duration, previous_trace_info, parent_trace))
    else:
        nesting_diff = current_nesting - previous_nesting
        if nesting_diff == 0:
            parent_trace.subtraces.append(
                _CmakeTrace(duration, previous_trace_info, parent_trace))
        elif nesting_diff < 0:
            for _ in range(nesting_diff, 0):
                assert parent_trace.parent_trace is not None
                parent_trace = parent_trace.parent_trace
            parent_trace.subtraces.append(
                _CmakeTrace(duration, previous_trace_info, parent_trace))
        elif nesting_diff == 1:
            parent_trace = parent_trace.subtraces[-1]
            parent_trace.subtraces.append(
                _CmakeTrace(duration, previous_trace_info, parent_trace))
        else:  # nesting_diff > 0
            assert nesting_diff <= 1, \
                    'Frames nesting increased by more than 1'

    if parent_trace.trace_info is None:
        traces.append(parent_trace.subtraces[-1])

    return parent_trace


def _parse_cmake_log(file_obj, ignore_nesting):
    matcher = re.compile(r'^\((?P<timestamp>[^)]*)\)\s*'
                         r'(\((?P<frame>[^)]*)\)\s*)?'
                         r'(?P<file>[^(]*)\((?P<line>[^)]*)\):\s*'
                         r'(?P<code>.*)$')

    def match_parens(code):
        nesting = 0
        for c in code:
            if c == '(':
                nesting += 1
            elif c == ')':
                nesting -= 1
        return nesting == 0

    # In cmake 3.10 at least `else` and `elseif` commands decrease nesting
    # level while they should not modify it at all. Once encounter such
    # commands we workaround this behavior increasing nesting level by 1.
    cmake_commands_with_nesting_bugs_re = re.compile(r'^(else|elseif)\s*\(')

    current_nesting = None
    current_timeval = None
    current_trace_info = None
    for line in file_obj:
        # Strip possible /r on Windows.
        line = line.rstrip()

        match = matcher.match(line)
        if match is None:
            if (current_trace_info is None or
                    match_parens(current_trace_info.cmake_code_line)):
                # We log to stderr all lines which don't match.
                print('Ignored: {}'.format(line), file=sys.stderr)
            else:
                current_trace_info.cmake_code_line += '\\n{}'.format(line)
            continue

        if current_trace_info is not None:
            yield (current_nesting, current_timeval, current_trace_info)

        cmake_code = match.group('code')

        if not ignore_nesting:
            current_nesting = match.group('frame')
            if current_nesting is not None:
                current_nesting = int(current_nesting)
                # Workaround cmake bugs.
                if cmake_commands_with_nesting_bugs_re.search(cmake_code):
                    current_nesting += 1

        current_timeval = float(match.group('timestamp'))
        current_trace_info = _CmakeTraceInfo(match.group('file'),
                                             int(match.group('line')),
                                             cmake_code)
    else:
        if current_trace_info is not None:
            yield (current_nesting, current_timeval, current_trace_info)


def _collect_stats(traces, file_obj, ignore_nesting):
    previous_nesting = 1
    previous_timeval = None
    previous_trace_info = None
    # With this variable we re-create nesting level of the command previous
    # to currently processed.
    previous_nesting_diff = 0

    parent_trace = _CmakeTrace(0, None, None)
    for (current_nesting, current_timeval,
         current_trace_info) in _parse_cmake_log(file_obj, ignore_nesting):
        if previous_trace_info is not None:
            parent_trace = _update_traces(
                traces, parent_trace, current_timeval, previous_timeval,
                previous_nesting, previous_nesting - previous_nesting_diff,
                previous_trace_info)

        if current_nesting is None:
            previous_nesting = 0
            previous_nesting_diff = 0
        else:
            previous_nesting_diff = current_nesting - previous_nesting
            previous_nesting = current_nesting

        previous_timeval = current_timeval
        previous_trace_info = current_trace_info
    else:
        if previous_trace_info is not None:
            # Add the last trace line with the smallest duration for
            # completeness. Hopefully, this line doesn't take too
            # long actually.
            _update_traces(traces, parent_trace,
                           previous_timeval + 10E-7, previous_timeval,
                           previous_nesting, previous_nesting,
                           previous_trace_info)


def _print_traces(args, ti_to_string, all_traces, whole_duration):
    def print_traces_loop(traces, indent):
        ordered_traces = traces
        if args.sort_traces:
            ordered_traces = sorted(traces, key=lambda x: x.duration,
                                    reverse=True)

        for trace in ordered_traces:
            if args.depth and indent + 1 > args.depth:
                break
            if trace.duration / whole_duration < args.threshold:
                break

            ti_str = ti_to_string(trace.trace_info,
                                  args.trace_info_width,
                                  str(indent + 1))

            print('{}{} ({}sec)({}%)'.format(
                ' ' * indent * _INDENT_STEP, ti_str, trace.duration,
                trace.duration / whole_duration * 100))

            print_traces_loop(trace.subtraces, indent + 1)

            if args.one and indent == 0:
                break

    print_traces_loop(all_traces, 0)


_StoredTrace = collections.namedtuple('_StoredTrace',
                                      ['trace_info', 'duration', 'subtraces'])


def _main(args):
    traces_key = 'traces'

    input_stream = sys.stdin
    if args.trace is not None:
        input_stream = open(args.trace)

    if os.path.exists(args.shelve_file) and not args.report_only:
        os.remove(args.shelve_file)

    traces_store = shelve.open(args.shelve_file)

    try:
        if args.report_only:
            all_traces = traces_store.get(traces_key, [])
        else:
            all_traces = []
            _collect_stats(all_traces, input_stream, args.ignore_nesting)

            def store_trace(trace):
                return _StoredTrace(trace.trace_info, trace.duration,
                                    [store_trace(t) for t in trace.subtraces])

            traces_store[traces_key] = [store_trace(t) for t in all_traces]
        traces_store.close()
    except:
        traces_store.close()
        if os.path.exists(args.shelve_file):
            os.remove(args.shelve_file)
        raise

    whole_duration = sum(t.duration for t in all_traces)

    trace_info_to_string = _CmakeTraceInfo.to_string_plain
    if args.trace_info_width is not None:
        trace_info_to_string = _CmakeTraceInfo.to_string_adjusted

    _print_traces(args, trace_info_to_string, all_traces, whole_duration)


if __name__ == '__main__':
    sys.exit(_main(_process_arguments()))
