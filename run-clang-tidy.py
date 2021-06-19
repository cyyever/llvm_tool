#!/usr/bin/env python
#
# ===- run-clang-tidy.py - Parallel clang-tidy runner --------*- python -*--===#
#
# Part of the LLVM Project, under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception
#
# ===-----------------------------------------------------------------------===#
# FIXME: Integrate with clang-tidy-diff.py


"""
Parallel clang-tidy runner
==========================

Runs clang-tidy over all files in a compilation database. Requires clang-tidy
 in $PATH.

Example invocations.
- Run clang-tidy on all files in the current working directory with a default
  set of checks and show warnings in the cpp files and all project headers.
    run-clang-tidy.py $PWD

- Fix all header guards.
    run-clang-tidy.py -fix -checks=-*,llvm-header-guard

- Fix all header guards included from clang-tidy and header guards
  for clang-tidy headers.
    run-clang-tidy.py -fix -checks=-*,llvm-header-guard extra/clang-tidy \
                      -header-filter=extra/clang-tidy

Compilation database setup:
http://clang.llvm.org/docs/HowToSetupToolingForLLVM.html
"""

from __future__ import print_function

import argparse
import json
import multiprocessing
import os
import queue
import re
import subprocess
import sys
import threading


def find_compilation_database(path):
    """Adjusts the directory until a compilation database is found."""
    result = "./"
    while not os.path.isfile(os.path.join(result, path)):
        if os.path.realpath(result) == "/":
            print("Error: could not find compilation database.")
            sys.exit(1)
        result += "../"
    return os.path.realpath(result)


def make_absolute(f, directory):
    if os.path.isabs(f):
        return f
    return os.path.normpath(os.path.join(directory, f))


def get_tidy_invocation(
    f,
    clang_tidy_binary,
    checks,
    fix,
    build_path,
    header_filter,
    allow_enabling_alpha_checkers,
    extra_arg,
    extra_arg_before,
    quiet,
    config,
    config_file,
    format_style,
):
    """Gets a command line for clang-tidy."""
    start = [clang_tidy_binary]
    if allow_enabling_alpha_checkers:
        start.append("-allow-enabling-analyzer-alpha-checkers")
    if header_filter is not None:
        start.append("-header-filter=" + header_filter)
    if checks:
        start.append("-checks=" + checks)
    if fix:
        start.append("-fix")
    for arg in extra_arg:
        start.append("-extra-arg=%s" % arg)
    for arg in extra_arg_before:
        start.append("-extra-arg-before=%s" % arg)
    start.append("-p=" + build_path)
    if quiet:
        start.append("-quiet")
    if config:
        start.append("-config=" + config)
    if config_file:
        start.append("-config-file=" + config_file)
    if format_style:
        start.append("-format-style=" + format_style)
    start.append(f)
    return start


def run_tidy(args, build_path, queue, lock, failed_files):
    """Takes filenames out of queue and runs clang-tidy on them."""
    while True:
        name = queue.get()
        invocation = get_tidy_invocation(
            name,
            args.clang_tidy_binary,
            args.checks,
            args.fix,
            build_path,
            args.header_filter,
            args.allow_enabling_alpha_checkers,
            args.extra_arg,
            args.extra_arg_before,
            args.quiet,
            args.config,
            args.config_file,
            args.format_style,
        )

        with subprocess.Popen(
            invocation, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as proc:
            output, err = proc.communicate()
            if proc.returncode != 0:
                failed_files.append(name)
            with lock:
                sys.stdout.write(output.decode("utf-8"))
                if len(err) > 0:
                    sys.stdout.flush()
                    sys.stderr.write(err.decode("utf-8"))
            queue.task_done()


def main():
    parser = argparse.ArgumentParser(
        description="Runs clang-tidy over all files "
        "in a compilation database. Requires "
        "clang-tidy in "
        "$PATH."
    )
    parser.add_argument(
        "-allow-enabling-alpha-checkers",
        action="store_true",
        help="allow alpha checkers from " "clang-analyzer.",
    )
    parser.add_argument(
        "-clang-tidy-binary",
        metavar="PATH",
        default="clang-tidy",
        help="path to clang-tidy binary",
    )
    parser.add_argument(
        "-checks",
        default=None,
        help="checks filter, when not specified, use clang-tidy " "default",
    )
    parser.add_argument(
        "-config",
        default=None,
        help="Specifies a configuration in YAML/JSON format: "
        "  -config=\"{Checks: '*', "
        "                       CheckOptions: [{key: x, "
        '                                       value: y}]}" '
        "When the value is empty, clang-tidy will "
        "attempt to find a file named .clang-tidy for "
        "each source file in its parent directories.",
    )
    parser.add_argument(
        "-config-file",
        default=None,
        help="Specify the path of .clang-tidy or custom config file"
        "Use either --config-file or --config, not both.",
    )
    parser.add_argument(
        "-format-style",
        default=None,
        help="Style for formatting code around applied fixes",
    )
    parser.add_argument(
        "-header-filter",
        default=None,
        help="regular expression matching the names of the "
        "headers to output diagnostics from. Diagnostics from "
        "the main file of each translation unit are always "
        "displayed.",
    )
    parser.add_argument(
        "-j",
        type=int,
        default=0,
        help="number of tidy instances to be run in parallel.",
    )
    parser.add_argument(
        "files", nargs="*", default=[".*"], help="files to be processed (regex on path)"
    )
    parser.add_argument(
        "-excluded-file-patterns",
        type=str,
        default=None,
        help="files to be excluded (regex on path)",
    )
    parser.add_argument("-fix", action="store_true", help="apply fix-its")
    parser.add_argument(
        "-format", action="store_true", help="Reformat code " "after applying fixes"
    )
    parser.add_argument(
        "-style",
        default="file",
        help="The style of reformat " "code after applying fixes",
    )
    parser.add_argument(
        "-p", dest="build_path", help="Path used to read a compile command database."
    )
    parser.add_argument(
        "-extra-arg",
        dest="extra_arg",
        action="append",
        default=[],
        help="Additional argument to append to the compiler " "command line.",
    )
    parser.add_argument(
        "-extra-arg-before",
        dest="extra_arg_before",
        action="append",
        default=[],
        help="Additional argument to prepend to the compiler " "command line.",
    )
    parser.add_argument(
        "-quiet", action="store_true", help="Run clang-tidy in quiet mode"
    )
    args = parser.parse_args()

    db_path = "compile_commands.json"

    if args.build_path is not None:
        build_path = args.build_path
    else:
        # Find our database
        build_path = find_compilation_database(db_path)

    try:
        invocation = [args.clang_tidy_binary, "-list-checks"]
        if args.allow_enabling_alpha_checkers:
            invocation.append("-allow-enabling-analyzer-alpha-checkers")
        invocation.append("-p=" + build_path)
        if args.checks:
            invocation.append("-checks=" + args.checks)
        invocation.append("-")
        if args.quiet:
            # Even with -quiet we still want to check if we can call clang-tidy.
            with open(os.devnull, "w") as dev_null:
                subprocess.check_call(invocation, stdout=dev_null)
        else:
            subprocess.check_call(invocation)
    except BaseException:
        print("Unable to run clang-tidy.", file=sys.stderr)
        sys.exit(1)

    # Load the database and extract all files.
    database = json.load(open(os.path.join(build_path, db_path)))
    files = {make_absolute(entry["file"], entry["directory"]) for entry in database}

    max_task = args.j
    if max_task == 0:
        max_task = multiprocessing.cpu_count()

    # Build up a big regexy filter from all command line arguments.
    file_name_re = re.compile("|".join(args.files))
    excluded_file_name_re = None
    if args.excluded_file_patterns is not None:
        excluded_file_name_re = re.compile(args.excluded_file_patterns)

    return_code = 0
    try:
        # Spin up a bunch of tidy-launching threads.
        task_queue = queue.Queue(max_task)
        # List of files with a non-zero return code.
        failed_files = []
        lock = threading.Lock()
        for _ in range(max_task):
            t = threading.Thread(
                target=run_tidy,
                args=(args, build_path, task_queue, lock, failed_files),
            )
            t.daemon = True
            t.start()

        # Fill the queue with files.
        files = {name for name in files if not name.endswith(".cu")}
        files = {name for name in files if not name.endswith(".cuh")}
        files = {name for name in files if file_name_re.search(name)}
        if excluded_file_name_re is not None:
            files = {name for name in files if not excluded_file_name_re.search(name)}

        print("Will check", len(files), "files")
        for name in files:
            task_queue.put(name)

        # Wait for all threads to be done.
        task_queue.join()
        if failed_files:
            return_code = 1

    except KeyboardInterrupt:
        # This is a sad hack. Unfortunately subprocess goes
        # bonkers with ctrl-c and we start forking merrily.
        print("\nCtrl-C detected, goodbye.")
        os.kill(0, 9)

    sys.exit(return_code)


if __name__ == "__main__":
    main()
