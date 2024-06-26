import os
import glob
import re
import shutil
import sqlalchemy
import traceback
import importlib

from rapidfuzz import fuzz
from traitlets.config import LoggingConfigurable, Config
from traitlets import Bool, List, Dict, Integer, Instance, Type, Any
from traitlets import default, validate
from textwrap import dedent
from nbconvert.exporters import Exporter, NotebookExporter
from nbconvert.writers import FilesWriter

from ..coursedir import CourseDirectory
from ..utils import find_all_files, rmtree, remove
from ..preprocessors.execute import UnresponsiveKernelError
from ..postprocessors import DuplicateCellError
from ..nbgraderformat import SchemaTooOldError, SchemaTooNewError
import typing
from nbconvert.exporters.exporter import ResourcesDict


class NbGraderException(Exception):
    pass


class BaseConverter(LoggingConfigurable):

    notebooks = List([])
    assignments = Dict({})
    writer = Instance(FilesWriter)
    exporter = Instance(Exporter)
    exporter_class = Type(NotebookExporter, klass=Exporter).tag(config=True)
    preprocessors = List([])

    force = Bool(False, help="Whether to overwrite existing assignments/submissions").tag(config=True)

    pre_convert_hook = Any(
        None,
        config=True,
        allow_none=True,
        help=dedent("""
        An optional hook function that you can implement to do some
        bootstrapping work before converting. 
        This function is called before the notebooks are converted
        and should be used for specific converters such as Autograde,
        GenerateAssignment or GenerateFeedback.

        It will be called as (all arguments are passed as keywords)::

            hook(assignment=assignment, student=student, notebooks=notebooks)
        """)
    )

    post_convert_hook = Any(
        None,
        config=True,
        allow_none=True,
        help=dedent("""
        An optional hook function that you can implement to do some
        work after converting. 
        This function is called after the notebooks are converted
        and should be used for specific converters such as Autograde,
        GenerateAssignment or GenerateFeedback.

        It will be called as (all arguments are passed as keywords)::

            hook(assignment=assignment, student=student, notebooks=notebooks)
        """)
    )

    permissions = Integer(
        help=dedent(
            """
            Permissions to set on files output by nbgrader. The default is
            generally read-only (444), with the exception of nbgrader
            generate_assignment and nbgrader generate_feedback, in which case
            the user also has write permission.
            """
        )
    ).tag(config=True)

    @default("permissions")
    def _permissions_default(self) -> int:
        return 664 if self.coursedir.groupshared else 444

    @validate('pre_convert_hook')
    def _validate_pre_convert_hook(self, proposal):
        value = proposal['value']
        if isinstance(value, str):
            module, function = value.rsplit('.', 1)
            value = getattr(importlib.import_module(module), function)
        if not callable(value):
            raise TraitError("pre_convert_hook must be callable")
        return value

    @validate('post_convert_hook')
    def _validate_post_convert_hook(self, proposal):
        value = proposal['value']
        if isinstance(value, str):
            module, function = value.rsplit('.', 1)
            value = getattr(importlib.import_module(module), function)
        if not callable(value):
            raise TraitError("post_convert_hook must be callable")
        return value

    coursedir = Instance(CourseDirectory, allow_none=True)

    def __init__(self, coursedir: CourseDirectory = None, **kwargs: typing.Any) -> None:
        self.coursedir = coursedir
        super(BaseConverter, self).__init__(**kwargs)
        if self.parent and hasattr(self.parent, "logfile"):
            self.logfile = self.parent.logfile
        else:
            self.logfile = None

        c = Config()
        c.Exporter.default_preprocessors = []
        self.update_config(c)

    def start(self) -> None:
        self.init_notebooks()
        self.writer = FilesWriter(parent=self, config=self.config)
        self.exporter = self.exporter_class(parent=self, config=self.config)
        for pp in self.preprocessors:
            self.exporter.register_preprocessor(pp)
        currdir = os.getcwd()
        os.chdir(self.coursedir.root)
        try:
            self.convert_notebooks()
        finally:
            os.chdir(currdir)

    @default("classes")
    def _classes_default(self):
        classes = super(BaseConverter, self)._classes_default()
        classes.append(FilesWriter)
        classes.append(Exporter)
        for pp in self.preprocessors:
            if len(pp.class_traits(config=True)) > 0:
                classes.append(pp)
        return classes

    @property
    def _input_directory(self):
        raise NotImplementedError

    @property
    def _output_directory(self):
        raise NotImplementedError

    def _format_source(self, assignment_id: str, student_id: str, escape: bool = False) -> str:
        return self.coursedir.format_path(self._input_directory, student_id, assignment_id, escape=escape)

    def _format_dest(self, assignment_id: str, student_id: str, escape: bool = False) -> str:
        return self.coursedir.format_path(self._output_directory, student_id, assignment_id, escape=escape)

    def init_notebooks(self) -> None:
        self.assignments = {}
        self.notebooks = []
        assignment_glob = self._format_source(self.coursedir.assignment_id, self.coursedir.student_id)
        for assignment in glob.glob(assignment_glob):
            notebook_glob = os.path.join(assignment, self.coursedir.notebook_id + ".ipynb")
            found = glob.glob(notebook_glob)
            if len(found) == 0:
                self.log.warning("No notebooks were matched by '%s'", notebook_glob)
                continue
            self.assignments[assignment] = found

        if len(self.assignments) == 0:
            msg = "No notebooks were matched by '%s'" % assignment_glob
            self.log.error(msg)

            assignment_glob2 = self._format_source("*", self.coursedir.student_id)
            found = glob.glob(assignment_glob2)
            if found:
                scores = sorted([(fuzz.ratio(assignment_glob, x), x) for x in found])
                self.log.error("Did you mean: %s", scores[-1][1])

            raise NbGraderException(msg)

    def init_single_notebook_resources(self, notebook_filename: str) -> typing.Dict[str, typing.Any]:
        regexp = re.escape(os.path.sep).join([
            self._format_source("(?P<assignment_id>.*)", "(?P<student_id>.*)", escape=True),
            "(?P<notebook_id>.*).ipynb"
        ])

        m = re.match(regexp, notebook_filename)
        if m is None:
            msg = "Could not match '%s' with regexp '%s'" % (notebook_filename, regexp)
            self.log.error(msg)
            raise NbGraderException(msg)

        gd = m.groupdict()

        self.log.debug("Student: %s", gd['student_id'])
        self.log.debug("Assignment: %s", gd['assignment_id'])
        self.log.debug("Notebook: %s", gd['notebook_id'])

        resources = {}
        resources['unique_key'] = gd['notebook_id']
        resources['output_files_dir'] = '%s_files' % gd['notebook_id']

        resources['nbgrader'] = {}
        resources['nbgrader']['student'] = gd['student_id']
        resources['nbgrader']['assignment'] = gd['assignment_id']
        resources['nbgrader']['notebook'] = gd['notebook_id']
        resources['nbgrader']['db_url'] = self.coursedir.db_url

        return resources

    def write_single_notebook(self, output: str, resources: ResourcesDict) -> None:
        # configure the writer build directory
        self.writer.build_directory = self._format_dest(
            resources['nbgrader']['assignment'], resources['nbgrader']['student'])

        # write out the results
        self.writer.write(output, resources, notebook_name=resources['unique_key'])

    def init_destination(self, assignment_id: str, student_id: str) -> bool:
        """Initialize the destination for an assignment. Returns whether the
        assignment should actually be processed or not (i.e. whether the
        initialization was successful).

        """
        if self.coursedir.student_id_exclude:
            exclude_ids = self.coursedir.student_id_exclude.split(',')
            if student_id in exclude_ids:
                return False

        dest = os.path.normpath(self._format_dest(assignment_id, student_id))

        # the destination doesn't exist, so we haven't processed it
        if self.coursedir.notebook_id == "*":
            if not os.path.exists(dest):
                return True
        else:
            # if any of the notebooks don't exist, then we want to process them
            for notebook in self.notebooks:
                filename = os.path.splitext(os.path.basename(notebook))[0] + self.exporter.file_extension
                path = os.path.join(dest, filename)
                if not os.path.exists(path):
                    return True

        # if we have specified --force, then always remove existing stuff
        if self.force:
            if self.coursedir.notebook_id == "*":
                self.log.warning("Removing existing assignment: {}".format(dest))
                rmtree(dest)
            else:
                for notebook in self.notebooks:
                    filename = os.path.splitext(os.path.basename(notebook))[0] + self.exporter.file_extension
                    path = os.path.join(dest, filename)
                    if os.path.exists(path):
                        self.log.warning("Removing existing notebook: {}".format(path))
                        remove(path)
            return True

        src = self._format_source(assignment_id, student_id)
        new_timestamp = self.coursedir.get_existing_timestamp(src)
        old_timestamp = self.coursedir.get_existing_timestamp(dest)

        # if --force hasn't been specified, but the source assignment is newer,
        # then we want to overwrite it
        if new_timestamp is not None and old_timestamp is not None and new_timestamp > old_timestamp:
            if self.coursedir.notebook_id == "*":
                self.log.warning("Updating existing assignment: {}".format(dest))
                rmtree(dest)
            else:
                for notebook in self.notebooks:
                    filename = os.path.splitext(os.path.basename(notebook))[0] + self.exporter.file_extension
                    path = os.path.join(dest, filename)
                    if os.path.exists(path):
                        self.log.warning("Updating existing notebook: {}".format(path))
                        remove(path)
            return True

        # otherwise, we should skip the assignment
        self.log.info("Skipping existing assignment: {}".format(dest))
        return False

    def init_assignment(self, assignment_id: str, student_id: str) -> None:
        """Initializes resources/dependencies/etc. that are common to all
        notebooks in an assignment.

        """
        source = self._format_source(assignment_id, student_id)
        dest = self._format_dest(assignment_id, student_id)

        # detect other files in the source directory
        for filename in find_all_files(source, self.coursedir.ignore + ["*.ipynb"]):
            # Make sure folder exists.
            path = os.path.join(dest, os.path.relpath(filename, source))
            if not os.path.exists(os.path.dirname(path)):
                os.makedirs(os.path.dirname(path))
            if os.path.exists(path):
                remove(path)
            self.log.info("Copying %s -> %s", filename, path)
            shutil.copy(filename, path)

    def set_permissions(self, assignment_id: str, student_id: str) -> None:
        self.log.info("Setting destination file permissions to %s", self.permissions)
        dest = os.path.normpath(self._format_dest(assignment_id, student_id))
        permissions = int(str(self.permissions), 8)
        for dirname, _, filenames in os.walk(dest):
            for filename in filenames:
                os.chmod(os.path.join(dirname, filename), permissions)
            # If groupshared, set dir permissions - see comment below.
            st_mode = os.stat(dirname).st_mode
            if self.coursedir.groupshared and st_mode & 0o2770 != 0o2770:
                try:
                    os.chmod(dirname, (st_mode|0o2770) & 0o2777)
                except PermissionError:
                    self.log.warning("Could not update permissions of %s to make it groupshared", dirname)
        # If groupshared, set write permissions on directories.  Directories
        # are created within ipython_genutils.path.ensure_dir_exists via
        # nbconvert.writer, (unless there are supplementary files) with a
        # default mode of 755 and there is no way to pass the mode arguments
        # all the way to there!  So we have to walk and fix.
        if self.coursedir.groupshared:
            # Root may be created in this step, and is not included above.
            rootdir = self.coursedir.format_path(self._output_directory, '.', '.')
            # Add 2770 to existing dir permissions (don't unconditionally override)
            st_mode = os.stat(rootdir).st_mode
            if st_mode & 0o2770 != 0o2770:
                try:
                    os.chmod(rootdir, (st_mode|0o2770) & 0o2777)
                except PermissionError:
                    self.log.warning("Could not update permissions of %s to make it groupshared", rootdir)

    def convert_single_notebook(self, notebook_filename: str) -> None:
        """
        Convert a single notebook.

        Performs the following steps:
            1. Initialize notebook resources
            2. Export the notebook to a particular format
            3. Write the exported notebook to file
        """
        self.log.info("Converting notebook %s", notebook_filename)
        resources = self.init_single_notebook_resources(notebook_filename)
        output, resources = self.exporter.from_filename(notebook_filename, resources=resources)
        self.write_single_notebook(output, resources)

    def convert_notebooks(self) -> None:
        errors = []

        def _handle_failure(gd: typing.Dict[str, str]) -> None:
            dest = os.path.normpath(self._format_dest(gd['assignment_id'], gd['student_id']))
            if self.coursedir.notebook_id == "*":
                if os.path.exists(dest):
                    self.log.warning("Removing failed assignment: {}".format(dest))
                    rmtree(dest)
            else:
                for notebook in self.notebooks:
                    filename = os.path.splitext(os.path.basename(notebook))[0] + self.exporter.file_extension
                    path = os.path.join(dest, filename)
                    if os.path.exists(path):
                        self.log.warning("Removing failed notebook: {}".format(path))
                        remove(path)

        for assignment in sorted(self.assignments.keys()):
            # initialize the list of notebooks and the exporter
            self.notebooks = sorted(self.assignments[assignment])

            # parse out the assignment and student ids
            regexp = self._format_source("(?P<assignment_id>.*)", "(?P<student_id>.*)", escape=True)
            m = re.match(regexp, assignment)
            if m is None:
                msg = "Could not match '%s' with regexp '%s'" % (assignment, regexp)
                self.log.error(msg)
                raise NbGraderException(msg)
            gd = m.groupdict()

            try:
                # determine whether we actually even want to process this submission
                should_process = self.init_destination(gd['assignment_id'], gd['student_id'])
                if not should_process:
                    continue

                self.run_pre_convert_hook()

                # initialize the destination
                self.init_assignment(gd['assignment_id'], gd['student_id'])

                # convert all the notebooks
                for notebook_filename in self.notebooks:
                    # The outer try-except is on an assignment level.
                    # If desired behavior for duplicate cells is to skip duplicates and autograde
                    # we need to catch errors on a notebook level
                    # So either do this (try-except again), or take the for loop out.
                    try:
                        self.convert_single_notebook(notebook_filename)
                    except DuplicateCellError:
                        msg = (
                            "Encountered a cell with duplicate id."
                        )
                        self.log.error(msg)
                        errors.append((gd['assignment_id'], gd['student_id']))

                # set assignment permissions
                self.set_permissions(gd['assignment_id'], gd['student_id'])
                self.run_post_convert_hook()

            except UnresponsiveKernelError:
                self.log.error(
                    "While processing assignment %s, the kernel became "
                    "unresponsive and we could not interrupt it. This probably "
                    "means that the students' code has an infinite loop that "
                    "consumes a lot of memory or something similar. nbgrader "
                    "doesn't know how to deal with this problem, so you will "
                    "have to manually edit the students' code (for example, to "
                    "just throw an error rather than enter an infinite loop). ",
                    assignment)
                errors.append((gd['assignment_id'], gd['student_id']))
                _handle_failure(gd)

            except sqlalchemy.exc.OperationalError:
                _handle_failure(gd)
                self.log.error(traceback.format_exc())
                msg = (
                    "There was an error accessing the nbgrader database. This "
                    "may occur if you recently upgraded nbgrader. To resolve "
                    "the issue, first BACK UP your database and then run the "
                    "command `nbgrader db upgrade`."
                )
                self.log.error(msg)
                raise NbGraderException(msg)

            except SchemaTooOldError:
                _handle_failure(gd)
                msg = (
                    "One or more notebooks in the assignment use an old version \n"
                    "of the nbgrader metadata format. Please **back up your class files \n"
                    "directory** and then update the metadata using:\n\nnbgrader update .\n"
                )
                self.log.error(msg)
                raise NbGraderException(msg)

            except SchemaTooNewError:
                _handle_failure(gd)
                msg = (
                    "One or more notebooks in the assignment use an newer version \n"
                    "of the nbgrader metadata format. Please update your version of \n"
                    "nbgrader to the latest version to be able to use this notebook.\n"
                )
                self.log.error(msg)
                raise NbGraderException(msg)

            except KeyboardInterrupt:
                _handle_failure(gd)
                self.log.error("Canceled")
                raise

            except Exception:
                self.log.error("There was an error processing assignment: %s", assignment)
                self.log.error(traceback.format_exc())
                errors.append((gd['assignment_id'], gd['student_id']))
                _handle_failure(gd)

        if len(errors) > 0:
            for assignment_id, student_id in errors:
                self.log.error(
                    "There was an error processing assignment '{}' for student '{}'".format(
                        assignment_id, student_id))

            if self.logfile:
                msg = (
                    "Please see the error log ({}) for details on the specific "
                    "errors on the above failures.".format(self.logfile))
            else:
                msg = (
                    "Please see the the above traceback for details on the specific "
                    "errors on the above failures.")

            self.log.error(msg)
            raise NbGraderException(msg)

    def run_pre_convert_hook(self):
        if self.pre_convert_hook:
            self.log.info('Running pre-convert hook')
            try:
                self.pre_convert_hook(
                    assignment=self.coursedir.assignment_id,
                    student=self.coursedir.student_id,
                    notebooks=self.notebooks)
            except Exception:
                self.log.info('Pre-convert hook failed', exc_info=True)

    def run_post_convert_hook(self):
        if self.post_convert_hook:
            self.log.info('Running post-convert hook')
            try:
                self.post_convert_hook(
                    assignment=self.coursedir.assignment_id,
                    student=self.coursedir.student_id,
                    notebooks=self.notebooks)
            except Exception:
                self.log.info('Post-convert hook failed', exc_info=True)