#!/usr/bin/env python
"""Setup analysis based on project identifiers

Usage:
  project_analysis_setup.py <YAML config file> <flow cell dir> <project dir>
                            [<YAML run information> --data_prefix=<data prefix>
                             --flowcell_alias=<flowcell alias>
                             --project_desc=<project_desc>
                             --lanes=<lanes> --move_data
                             --only_install_run_info --only_install_fastq
                             --dry_run --verbose]


Given a directory with demultiplexed flow cell data, and a project id
or a comma-separated list of lane numbers, project specific files will
be copied to a project directory. The optional <YAML run information>
file specifies details about the flowcell lanes, instead of retrieving
it from Galaxy. See automated_initial_analysis.py for details.

For a multiproject run_info file, only a subset of the lanes can be
used. The run_info file is therefore pruned, and the pruned file is
output to the project directory. The pruning is based on the options
<project_desc>, or <lanes>. Keyword ALL delivers all lanes.


Options:
  -d, --data_prefix=<data_prefix>               Install flowcells in <project_dir>/<data_prefix>
  -a, --flowcell_alias=<flowcell alias>         By default, samples are moved to a directory named
                                                <flowcell_id>. This option changes output directory to
                                                <flowcell_alias>.
  -y, --project_desc=<project_desc>             Project description in description field of run_info file, or ALL.
  -l, --lanes=<lanes>                           Comma-separated list of integers corresponding to lanes
  -i, --only_install_run_info                   Only install pruned run_info file.
  -f, --only_install_fastq                      Only install fastq files.
  -m, --move_data                               Move data instead of copying
  -n, --dry_run                                 Don't do anything samples, just list what will happen
  -v, --verbose                                 Print some more information
"""

import os
import sys
from optparse import OptionParser

import yaml
import glob
import shutil
from itertools import izip

from bcbio.log import create_log_handler
from bcbio.pipeline import log
from bcbio.pipeline.run_info import get_run_info, prune_run_info_by_description
from bcbio.pipeline.lane import get_flowcell_id
from bcbio.pipeline.fastq import get_single_fastq_files, get_barcoded_fastq_files, convert_barcode_id_to_name, get_fastq_files
from bcbio.pipeline.config_loader import load_config
from bcbio import utils

def main(config_file, fc_dir, project_dir, run_info_yaml=None, fc_alias=None, project_desc=None, lanes=None):
    if project_desc is None and lanes is None:
        log.error("No project description or lanes provided: cannot deliver files without this information")
        sys.exit()

    config = load_config(config_file)
    ## Set log file in project output directory
    config.update(log_dir=os.path.join(project_dir, "log"))
    log_handler = create_log_handler(config, log.name)

    fc_dir = os.path.normpath(fc_dir)
    fc_name, fc_date, run_info = get_run_info(fc_dir, config, run_info_yaml)
    with log_handler.applicationbound():
        run_info = prune_run_info_by_description(run_info['details'], project_desc, lanes)
    if len(run_info) == 0:
        log.error("No lanes found with matching description %s: please check your flowcell run information" % project_desc)
        sys.exit()

    dirs = dict(fc_dir=fc_dir, project_dir=project_dir)
    fc_name, fc_date = get_flowcell_id(run_info, dirs['fc_dir'])
    config.update(fc_name = fc_name, fc_date = fc_date)
    config.update(fc_alias = "%s_%s" % (fc_date, fc_name) if not fc_alias else fc_alias)
    dirs.update(fc_delivery_dir = os.path.join(dirs['project_dir'], options.data_prefix, config['fc_alias'] ))
    dirs.update(data_delivery_dir = os.path.join(dirs['project_dir'], options.data_prefix, "%s_%s" %(fc_date, fc_name) ))
    with log_handler.applicationbound():
        config = _make_delivery_directory(dirs, config)
        _save_run_info(run_info, dirs['fc_delivery_dir'], run_exit=options.only_run_info)
        run_main(run_info, config, dirs)

def run_main(run_info, config, dirs):
    for info in run_info:
        process_lane(info, config, dirs)

def process_lane(info, config, dirs):
    """Models bcbio process lane"""
    sample_name = info.get("description", "")
    genome_build = info.get("genome_build", None)
    multiplex = info.get('multiplex', None)
    log.info("Processing sample: %s; lane %s; reference genome %s" %
             (sample_name, info["lane"], genome_build))
    if multiplex:
        log.debug("Sample %s is multiplexed as: %s" % (sample_name, multiplex))
    fq = get_barcoded_fastq_files(multiplex, info, dirs['fc_dir'], config['fc_name'], config['fc_date'])
    
    ## Move data along with fastq files
    fc_bc_dir = os.path.join(config['data_delivery_dir'], "%s_%s_%s_barcode" % (info['lane'], config['fc_date'], config['fc_name']))
    _make_dir(fc_bc_dir, "fastq.txt barcode directory")
    if not options.only_fastq:
        data, fastqc = _get_analysis_results(config, dirs, info['lane'])
        _deliver_data(data, fastqc, config['data_delivery_dir'])

    for fqpair in fq:
        [_deliver_fastq_file(fq_src, os.path.basename(fq_src), fc_bc_dir) for fq_src in fqpair]


def _deliver_fastq_file(fq_src, fq_tgt, outdir, fc_link_dir=None):
    _handle_data(fq_src, os.path.join(outdir, fq_tgt), f=shutil.move if options.move else shutil.copyfile)

def _make_delivery_directory(dirs, config):
    """Make the output directory"""
    _make_dir(dirs['fc_delivery_dir'], "flowcell delivery")
    _make_dir(dirs['data_delivery_dir'], "data delivery")
    if (os.path.basename(dirs['data_delivery_dir']) != config['fc_alias']):
        _handle_data(dirs['data_delivery_dir'], os.path.join(os.path.dirname(dirs['data_delivery_dir']), config['fc_alias']), os.symlink)
    config.update(fc_delivery_dir=dirs['fc_delivery_dir'])
    config.update(data_delivery_dir=dirs['data_delivery_dir'])
    return config

def _make_dir(dir, label):
    if not os.path.exists(dir):
        os.makedirs(dir)
        log.info("Creating %s directory %s" % (label, dir))
    else:
        log.warn("%s already exists: not creating new directory" % (dir))

def _handle_data(src, tgt, f=shutil.copyfile):
    if src is None:
        return
    if os.path.exists(tgt):
        log.warn("%s already exists: not doing anything!" %(tgt))
        return
    if options.dry_run:
        print "DRY_RUN: %s file %s to %s" % (f.__name__, src, tgt)
    else:
        log.info("%s file %s to %s" % (f.__name__, src, tgt))
        f(src, tgt)

def _deliver_data(data, fastqc, outdir):
    """Loop over data and fastqc and deliver files"""
    for src in data:
        tgt = os.path.join(outdir, os.path.basename(src))
        _handle_data(src, tgt, f=shutil.move if options.move else shutil.copyfile)

    for src in fastqc:
        tgt = os.path.join(outdir, "fastqc", os.path.basename(src))
        _handle_data(src, tgt, f=shutil.move if options.move else shutil.copytree)
        
def _get_analysis_results(config, dirs, lane):
    """Get analysis results

    For now just glob the analysis directory for fastqc output and files with the give flowcell name
    """
    flowcell = "_".join([str(lane), config['fc_date'], config['fc_name']])
    glob_str = os.path.join(dirs['fc_dir'], flowcell + "*.*")
    data = glob.glob(glob_str)
    glob_str = os.path.join(dirs['fc_dir'], "fastqc", flowcell + "*")
    fastqc = glob.glob(glob_str)
    return data, fastqc

def _save_run_info(run_info, outdir, run_exit=False):
    outfile = os.path.join(outdir, "project_run_info.yaml")
    if not options.dry_run:
        with open(outfile, "w") as out_handle:
            yaml.dump(run_info, stream=out_handle)
    else:
        print "DRY_RUN:"
        yaml.dump(run_info, stream=sys.stdout)
    if run_exit:
        sys.exit()


if __name__ == "__main__":
    usage = """
    project_analysis_setup.py <YAML config file> <flow cell dir> <project dir>
                            [<YAML run information> --data_prefix=<data prefix>
                             --flowcell_alias=<flowcell alias>
                             --project_desc=<project_desc>
                             --lanes=<lanes> --move_data
                             --only_install_run_info --only_install_fastq
                             --dry_run --verbose]

    For more extensive help type project_analysis_setup.py
"""

    parser = OptionParser(usage=usage)
    parser.add_option("-d", "--data_prefix", dest="data_prefix",
                      default="")
    parser.add_option("-a", "--flowcell_alias", dest="fc_alias")
    parser.add_option("-y", "--project_desc", dest="project_desc")
    parser.add_option("-l", "--lanes", dest="lanes")

    parser.add_option("-i", "--only_install_fastq", dest="only_fastq", action="store_true",
                      default=False)
    parser.add_option("-f", "--only_install_run_info", dest="only_run_info", action="store_true",
                      default=False)
    parser.add_option("-m", "--move_data", dest="move", action="store_true",
                      default=False)
    parser.add_option("-v", "--verbose", dest="verbose", action="store_true",
                      default=False)
    parser.add_option("-n", "--dry_run", dest="dry_run", action="store_true",
                      default=False)
    (options, args) = parser.parse_args()
    if len(args) < 3:
        print __doc__
        sys.exit()
    kwargs = dict(
        fc_alias = options.fc_alias,
        project_desc = options.project_desc,
        lanes = options.lanes
        )
    main(*args, **kwargs)
