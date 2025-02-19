#!/usr/bin/env python
# -*- coding: utf-8 -*-

#   Cypress -- A C++ interface to PyNN
#   Copyright (C) 2016 Andreas Stöckel
#
#   This program is free software: you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation, either version 3 of the License, or
#   (at your option) any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#
#   You should have received a copy of the GNU General Public License
#   along with this program.  If not, see <http://www.gnu.org/licenses/>.

# This script uploads an executable to the NMPI, executes it on the specified
# platform, collectes all files generated by the executable and puts them into
# the current directory, just as if the executable had ben executed locally.

import argparse
import base64
import bz2
import logging
import json
import os
import random
import shutil
import stat
import string
import sys
import tarfile
import time

try:
    from urlparse import urlparse
    from urllib import urlretrieve
except ImportError:  # Py3
    from urllib.parse import urlparse
    from urllib.request import urlretrieve

# Required as the Python code is usually concatenated into a single file and
# embedded in the Cypress C++ library.
try:
    path = os.path.join(os.path.dirname(__file__),
                        "lib/nmpi")
    if os.path.exists(os.path.join(path, "nmpi_user.py")):
        sys.path.append(path)
        from nmpi_user import *
except ImportError:
    pass

# Setup the logger
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(name)s:%(levelname)s:%(message)s"))
logger = logging.getLogger("cypress")
logger.setLevel(logging.INFO)
logger.addHandler(handler)

#
# Parse the command line
#
parser = argparse.ArgumentParser(
    description="Command line interface to the Python part of Cypress")
parser.add_argument("--executable", type=str, action="store", required=True,
                    help="File to be executed on the NMPI")
parser.add_argument("--platform", type=str, action="store", required=True,
                    help="Target platform (e.g. NM-PM1, NM-MC1, Spikey, ESS)")
parser.add_argument("--files", type=str, action="store", default=[], nargs='*',
                    help="List of additional files to be uploaded to the NMPI")
parser.add_argument("--base", type=str, action="store", default=os.getcwd(),
                    help="Base directory used when determining to which " +
                         "directory the files should be extracted on the NMPI")
parser.add_argument("--args", type=str, action="store", default=[], nargs='*',
                    help="Arguments to be passed to the executable")
parser.add_argument("--wafer", type=int, default=0,
                    help="Wafer for reservation")


args = parser.parse_args()

# Create a python script which contains all the specified files and extracts
# them to a directory upon execution


def tmpdirname(N):
    return ''.join(random.choice(string.ascii_uppercase +
                                 string.ascii_lowercase + string.digits) for _ in range(N))


def file_script(filename, tar_filename, execute):
    if not os.path.isfile(filename):
        return ""
    with open(filename, 'r') as fd:
        compressed = base64.b64encode(bz2.compress(fd.read()))
    return ("extract('" + tar_filename
            + "', 0o" + oct(os.stat(filename)[stat.ST_MODE])
            + ", '" + compressed + "')\n")

tmpdir = "cypress_" + tmpdirname(8)
script = """

# Automatically generated by Cypress

import base64
import bz2
import errno
import os
import shutil
import subprocess
import sys
import tarfile

# Remember which files were extracted -- we'll cleanup our traces after running
dir = os.path.realpath(os.path.join(os.getcwd(), '""" + tmpdir + """'))
files = []

# http://stackoverflow.com/questions/600268/mkdir-p-functionality-in-python
def mkdir_p(path):
    try:
        if (path != ""):
            os.makedirs(path)
    except OSError as exc:  # Python >2.5
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise

def setup():
    # List files in the current directory and link them into the target
    # directory
    for filename in os.listdir("."):
        source = os.path.realpath(os.path.join(".", filename))
        target = os.path.join(dir, filename)
        if target == dir or os.path.exists(target):
            continue
        os.symlink(source, target)

        # Important! Unlink file before recursively deleting subdirectory
        files.append(target)

def extract(filename, mode, data):
    filename = os.path.join(dir, filename)
    files.append(filename)
    mkdir_p(os.path.dirname(filename))
    with open(filename, 'w') as fd:
        fd.write(bz2.decompress(base64.b64decode(data)))
    os.chmod(filename, mode)

def run(filename, args):
    old_cwd = os.getcwd()
    res = 1
    try:
        os.chdir(dir)
        with open(os.path.join(dir, '"""  + tmpdir + """.stdout'), 'wb') as out, open(os.path.join(dir, '"""  + tmpdir + """.stderr'), 'wb') as err:
            p = subprocess.Popen([os.path.join(dir, filename)] + args,
                stdout = out, stderr = err)
            p.communicate()
            res = p.returncode
    finally:
        os.chdir(old_cwd)
    return res

def cleanup():
    pass
    # Remove extracted files -- we're only interested in newly created files
    #for file in files:
    #    try:
    #        os.unlink(file)
    #    except:
    #        pass

    # Create a tar.bz2 of the target folder containing all the output
    tarname = os.path.basename(dir)
    archive = tarname + ".tar.bz2"
    with tarfile.open(archive, "w:bz2") as tar:
        tar.add(dir, arcname=tarname)

    # Remove the target directory
    shutil.rmtree(dir)
"""

files = args.files + [args.executable]
for filename in files:
    tar_filename = os.path.relpath(filename, args.base)
    if (tar_filename.startswith("..")):
        raise Exception(
            "Base directory must be a parent directory of all specified files!")
    script = script + file_script(filename,
                                  tar_filename,
                                  filename == args.executable)

arguments = []
for arg in args.args:
    if os.path.exists(arg) and os.path.isfile(arg):
        arguments.append(os.path.relpath(arg, args.base))
    else:
        arguments.append(arg)

script = script + "setup()\n"
script = script + ("res = run('" + os.path.relpath(filename, args.base)
                   + "', " + str(arguments) + ")\n")
script = script + "cleanup()\n"
script = script + "sys.exit(res)\n"

#
# Read the NMPI client configuration
#
config = {}
config_file = os.path.expanduser(os.path.join("~", ".nmpi_config"))
if os.path.isfile(config_file):
    try:
        with open(config_file, 'r') as fd:
            config = json.load(fd)
    except Exception as e:
        logger.error(e.message)
        logger.warning(
            "Error while parsing ~/.nmpi_config. Starting with empty configuration!")
else:
    logger.warning(
        "~/.nmpi_config not found. Starting with empty configuration!")

# Prompt the project name
if not "collab_id" in config:
    config["collab_id"] = raw_input("Collab ID: ")

# Prompt the username
if not "username" in config:
    config["username"] = raw_input("Username: ")

# Create the client instance
token = config["token"] if "token" in config else None
while True:
    if token is None:
        logger.info(
            "No valid access token found or the access token has expired. Please re-enter your password to obtain a new access token.")

    sys.stdout.flush()
    sys.stderr.flush()

    time.sleep(0.1)

    # Submit the job, if this fails, explicitly query the password
    try:
        client = Client(username=config["username"], token=token)
        config["token"] = client.token

        # Save the configuration, including the current client token
        with open(config_file, 'w') as fd:
            json.dump(config, fd, indent=4)
        hw_config = {}
        if(args.wafer != 0):
            hw_config = {"WAFER_MODULE" : args.wafer}

        job_id = client.submit_job(
            source=script,
            platform=args.platform,
            config=hw_config,
            collab_id=config["collab_id"])
        job_id = str(job_id).split("/")[-1]
        logger.info(
            "Created job with ID " +
            str(job_id) +
            ", you can go to https://nmpi.hbpneuromorphic.eu/app/#/queue/"
            + str(job_id) +
            " to retrieve the job results")
    except:
        if token is not None:
            token = None
            continue
        else:
            raise
    break

# Wait until the job has switched to either the "error" or the "finished" state
status = ""
while True:
    new_status = client.job_status(job_id)
    if new_status != status:
        logger.info("Job status: " + new_status)
        status = new_status
    if status == "error" or status == "finished":
        break
    time.sleep(1)

# Download the result archive
job = client.get_job(job_id)
datalist = job["output_data"]
for dataitem in datalist:
    url = dataitem["url"]
    (scheme, netloc, path, params, query, fragment) = urlparse(url)
    archive = tmpdir + ".tar.bz2"
    if archive in path:
        # Download the archive containing the result data
        logger.info("Downloading result...")
        urlretrieve(url, archive)

        # Extract the output to the temporary directory
        logger.info("Extracting data...")
        with tarfile.open(archive, "r:*") as tar:
            members = [member for member in tar.getmembers(
            ) if member.name.startswith(tmpdir)]
            tar.extractall(members=members)
        os.unlink(archive)

        # Move the content from the temporary directory to the top-level
        # directory, remove the temporary directory
        for filename in os.listdir(tmpdir):
            src = os.path.join(tmpdir, filename)
            dest = os.path.join(os.getcwd(), filename)
            try:
                if not os.path.isdir(src):
                    shutil.copy(src, dest)
            except: 
                pass
        shutil.rmtree(tmpdir)

        logger.info("Done!")
        break

# Output both stdout and stderr
if os.path.isfile(tmpdir + ".stderr") and os.stat(tmpdir + ".stderr").st_size > 0:
    logger.info("Response stderr (" + tmpdir + ".stderr)")
    with open(tmpdir + ".stderr") as f:
        for s in f:
            sys.stderr.write(s)
if os.path.isfile(tmpdir + ".stdout") and os.stat(tmpdir + ".stdout").st_size > 0:
    logger.info("Response stdout (" + tmpdir + ".stdout)")
    with open(tmpdir + ".stdout") as f:
        for s in f:
            sys.stdout.write(s)

# Exit with the correct status
sys.exit(0 if status == "finished" else 1)

