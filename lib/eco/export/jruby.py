# Copyright (c) 2015--2016 King's College London
# Created by the Software Development Team <http://soft-dev.org/>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to
# deal in the Software without restriction, including without limitation the
# rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
# sell copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
# IN THE SOFTWARE.

import logging
import os
import os.path
import tempfile
import subprocess

from incparser.annotation import Annotation, Footnote, ToolTip, Railroad
from incparser.annotation import HUDEval, HUDTypes, HUDCallgraph

from incparser.astree import EOS
from grammar_parser.gparser import MagicTerminal, IndentationTerminal

from PyQt4.QtCore import QSettings


class JRubyCallGraph(Annotation):
    """Annotation for JRuby callgraph railroad diagrams."""

    def __init__(self, annotation):
        self._hints = [Railroad()]
        super(JRubyCallGraph, self).__init__(annotation)

    def has_hint(self, klass):
        if klass == Railroad:
            return True
        return False

    def get_hints(self):
        return self._hints


class JRubyMorphismMsg(Annotation):
    """Annotation for JRuby callgraph tooltips."""

    def __init__(self, annotation):
        self._hints = [ToolTip(), HUDCallgraph()]
        super(JRubyMorphismMsg, self).__init__(annotation)

    def has_hint(self, klass):
        if klass in (ToolTip, HUDCallgraph):
            return True
        return False

    def get_hints(self):
        return self._hints


class JRubyArgumentTypes(Annotation):
    """Annotation for JRuby type information."""

    def __init__(self, annotation):
        self._hints = [Footnote(), HUDTypes()]
        super(JRubyArgumentTypes, self).__init__(annotation)

    def has_hint(self, klass):
        if klass in (Footnote, HUDTypes):
            return True
        return False

    def get_hints(self):
        return self._hints


class JRubyEvalStrings(Annotation):
    """Annotation for JRuby type information."""

    def __init__(self, annotation):
        self._hints = [Footnote(), HUDEval()]
        super(JRubyEvalStrings, self).__init__(annotation)

    def has_hint(self, klass):
        if klass in (Footnote, HUDEval):
            return True
        return False

    def get_hints(self):
        return self._hints


class Source(object):
    """JRuby source file (needed by callgraph profiler)."""

    def __init__(self, file, line_start, line_end):
        self.file = file
        self.line_start = int(line_start)
        self.line_end = int(line_end)


class Method(object):
    """JRuby method (needed by callgraph profiler)."""

    def __init__(self, id_, name, source):
        self.id = int(id_)
        self.name = name
        self.source = source
        self.versions = []
        self.callsites = []
        self.is_mega = False

    def is_library(self):
        return "jruby/lib/" in self.source.file

    def is_core(self):
        return ("/core/" in self.source.file or "core.rb" in self.source.file
                or self.source.file == "(unknown)")

    def is_hidden(self):
        return (self.source.file == "run_jruby_root" or
                self.source.file == "context" or
                self.name == "Truffle::Boot#run_jruby_root" or
                self.name == "Truffle::Boot#context")

    def reachable(self):
        return self.callsites

    def __str__(self):
        return ("Method: %s id=%g callsites=[ %s ]" %
                (self.name, self.id, " ".join([str(cs) for cs in self.callsites])))


class MethodVersion(object):

    def __init__(self, id_, method):
        self.id = int(id_)
        self.method = method
        self.callsite_versions = []
        self.called_from = []
        self.eval_code = []
        self.arg_types = {}

    def reachable(self):
        # called_from isn't reachable
        return [self.method] + self.callsite_versions

    def __str__(self):
        types = "Arguments had types:" if len(self.arg_types) > 0 else ""
        for name in self.arg_types:
            types += "%s : %s\n" % (name, ", ".join(self.arg_types[name]))
        evals = ""
        if len(self.eval_code) > 0:
            evals = "Evaluated from:" + "\n\t".join(self.eval_code)
        return ("Method Version: %s id=%g called_from=[ %s ] %s" %
                (self.method.name, self.id,
                 ", ".join([str(cs) for cs in self.called_from]),
                 types, evals))

class CallSite(object):
    """JRuby callsite (needed by callgraph profiler)."""

    def __init__(self, id_, method, line):
        self.id = int(id_)
        self.method = method
        self.line = int(line)
        self.versions = []

    def reachable(self):
        return [self.method] + self.versions

    def __str__(self):
        return ("Callsite: %s id=%g line=%g" %
                (self.method.name, self.id, self.line))


class CallSiteVersion(object):
    """JRuby callsite version (needed by callgraph profiler)."""

    def __init__(self, id_, callsite, method_version):
        self.id = int(id_)
        self.callsite = callsite
        self.method_version = method_version
        self.calls = []

    def reachable(self):
        """Method_version isn't reachable - find it through calls."""
        return [self.callsite] + self.calls

    def __str__(self):
        return ("Callsite Version: %s id=%g" %
                (self.callsite.method.name, self.id))


class Mega(object):
    """Class used to indicate that a method is megamorphic."""

    def __str__(self):
        return "Megamorphic"


class Foreign(object):
    """Indicate that a method was defined in a language other than JRuby."""

    def __str__(self):
        return "Foreign function"


class JRubyExporter(object):
    """Export, run or profile a JRuby file."""

    def __init__(self, tm):
        self.tm = tm  # TreeManager object.
        self.sl_functions = {}
        self._sl_output = []
        self._wrappers = []
        self._output = []
        self._sl_functions = []

    def export(self, path=None, run=False, profile=False):
        if run:
            return self._run()
        elif profile:
            return self._profile(path=path)
        elif path is not None:
            self._export_as_text(path)
            return

    def _language_box(self, name, node):
        if name == "<Ruby>":
            self._walk_rb(node)

    def _walk_rb(self, node):
        while True:
            node = node.next_term
            sym = node.symbol
            if isinstance(node, EOS):
                break
            if isinstance(sym, MagicTerminal):
                self._language_box(sym.name, node.symbol.ast.children[0])
            elif isinstance(sym, IndentationTerminal):
                self._output.append(sym)
            elif sym.name == "\r":
                self._output.append("\n")
            else:
                self._output.append(sym.name)

    def _export_as_text(self, path):
        node = self.tm.lines[0].node # first node
        self._walk_rb(node)
        output = "".join(self._output)
        with open(path, "w") as fp:
            fp.write("".join(output))

    def _run(self):
        f = tempfile.mkstemp(suffix=".rb")
        settings = QSettings("softdev", "Eco")
        jruby_bin =str (settings.value("env_jruby").toString())
        self._export_as_text(f[1])
        return subprocess.Popen([jruby_bin, "-X+T", f[1]],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                bufsize=0)

    def _profile(self, path):
        callgraph_processor = JRubyCallgraphProcessor(self.tm)

        _, src_file_name = tempfile.mkstemp(suffix=".rb")
        self.tm.export_as_text(src_file_name).split("\n")

        log_file_name = os.path.join("/", "tmp",
                                     next(tempfile._get_candidate_names()) + ".txt")
        logging.debug("Placing callgraph trace in %s" % log_file_name)

        settings = QSettings("softdev", "Eco")
        jruby_bin = str(settings.value("env_jruby", "").toString())
        directory = str(settings.value("env_jruby_load", "").toString())
        if directory:
            load_path = "-I" + directory
        else:
            load_path = ""
        pic_size = str(settings.value("graalvm_pic_size", "").toString())
        cmd = [jruby_bin, "-X+T", "-J-Djvmci.Compiler=graal",
               "-Xtruffle.callgraph=true", load_path,
               "-Xtruffle.callgraph.write=" + log_file_name,
               "-Xtruffle.dispatch.cache=" + pic_size,
               src_file_name]
        logging.debug("Running command: %s" % " ".join(cmd))
        settings = QSettings("softdev", "Eco")
        graalvm_bin = str(settings.value("env_graalvm", "").toString())
        subprocess.call(cmd, env={"JAVACMD":graalvm_bin})

        return callgraph_processor.annotate_tree(src_file_name, log_file_name)


class JRubyCallgraphProcessor(object):
    """Process a JRuby callgraph log and annotate the current parse tree."""

    def __init__(self, tm):
        self.tm = tm

    def remove_all_annotations(self):
        temp_cursor = self.tm.cursor.copy()
        temp_cursor.line = 1
        temp_cursor.move_to_x(0)
        node = temp_cursor.find_next_visible(temp_cursor.node)
        while True:
            if isinstance(node, EOS):
                break
            for klass in (JRubyCallGraph, JRubyMorphismMsg, JRubyArgumentTypes):
                node.remove_annotations_by_class(klass)
            node = node.next_term

    def annotate_tree(self, src_file_name, log_file_name):
        """Run JRuby and dump a callgraph to disk.
        Parse the callgraph and annotate the syntax tree.
        """
        objects = dict()
        with open(log_file_name) as fd:
            output = fd.read()
            lines = output.split("\n")
            i = 0
            while i < len(lines):
                line = lines[i]
                tokens = line.split()
                if len(tokens) == 0:
                    pass
                elif tokens[0] == "method":
                    method = Method(tokens[1], " ".join(tokens[2:-3]), Source(*tokens[-3:]))
                    objects[method.id] = method
                elif tokens[0] == "method-version":
                    method = objects[int(tokens[1])]
                    method_version = MethodVersion(tokens[2], method)
                    objects[method_version.id] = method_version
                    method.versions.append(method_version)
                elif tokens[0] == "local":
                    method_version = objects[int(tokens[1])]
                    if tokens[2] not in method_version.arg_types:
                        method_version.arg_types[tokens[2]] = []
                    method_version.arg_types[tokens[2]].append(tokens[3])
                elif tokens[0] == "eval":
                    method_version = objects[int(tokens[1])]
                    eval_code = " ".join(tokens[2:])
                    method_version.eval_code.append(eval_code)
                elif tokens[0] == "callsite":
                    method = objects[int(tokens[1])]
                    callsite = CallSite(tokens[2], method, tokens[3])
                    objects[callsite.id] = callsite
                    method.callsites.append(callsite)
                elif tokens[0] == "callsite-version":
                    callsite = objects[int(tokens[1])]
                    method_version = objects[int(tokens[2])]
                    callsite_version = CallSiteVersion(tokens[3], callsite, method_version)
                    objects[callsite_version.id] = callsite_version
                    callsite.versions.append(callsite_version)
                    method_version.callsite_versions.append(callsite_version)
                elif tokens[0] == "calls":
                    callsite_version = objects[int(tokens[1])]
                    if tokens[2] == "mega":
                        callsite_version.calls.append(Mega())
                    elif tokens[2] == "foreign":
                        callsite_version.calls.append(Foreign())
                    else:
                        # We just store the method id here for now as we may not have seen all methods yet
                        callsite_version.calls.append(int(tokens[2]))
                else:
                    logging.debug("Cannot parse the following: %s" % line)
                    return
                i += 1

        # Resolve method ids to point to the actual object
        for obj in objects.itervalues():
            if isinstance(obj, CallSiteVersion):
                callsite_version = obj
                new_calls = []
                for call in callsite_version.calls:
                    if isinstance(call, Mega):
                        new_calls.append(Mega())
                        callsite_version.method_version.method.is_mega = True
                    elif isinstance(call, Foreign):
                        callsite_version.method_version.method.is_foreign = True
                    else:
                        called = objects[call]
                        called.called_from.append(callsite_version)
                        new_calls.append(called)
                callsite_version.calls = new_calls
        # Resolve eval() strings. This must be done after resolving method
        # ids, because we need to walk eval strings back up the callgraph.
        for obj in objects.itervalues():
            if isinstance(obj, MethodVersion) and "#eval" in obj.method.name:
                version = obj
                if version.eval_code:
                    for caller in version.called_from:
                        caller.method_version.eval_code.extend(version.eval_code)

        # Find which objects were actually used
        reachable_objects = set()
        reachable_worklist = set()
        for obj in objects.itervalues():
            if ((isinstance(obj, Method) and not obj.is_core()) or
                (isinstance(obj, MethodVersion) and
                 obj.method.name == "<main>" and
                 not obj.method.is_core())):
                reachable_worklist.add(obj)

        while len(reachable_worklist) > 0:
            obj = reachable_worklist.pop()
            if isinstance(obj, Mega):
                continue
            elif obj in reachable_objects:
                continue
            else:
                reachable_objects.add(obj)
                reachable_worklist.update(obj.reachable())

        # Process graph of reachable objects and annotate parse tree.
        self.remove_all_annotations()

        for obj in reachable_objects:
            if (isinstance(obj, Method) and obj.source.file != "(unknown)" and
                not obj.is_hidden() and not obj.is_core()
                and not "jruby/lib/" in obj.source.file
                and obj.source.file != "(eval)"):
                method = obj
                def_msg = method.name
                if method.is_mega:
                    def_msg += " is megamorphic."
                num_versions = len(method.versions)
                def_lineno = method.source.line_start
                def_filename = method.source.file
                # For now, if a method is not defined in the currently open
                # file, we ignore it. We also ignore methods like <main>
                # or times which were inserted by the interpreter.
                if (def_filename != src_file_name or
                      method.name.startswith("<") or
                      method.source.line_start < 0):
                    continue
                def_msg += "\n%s has %d versions" % (method.name, num_versions)
                def_arg_types = {}  # str -> set (string)
                def_eval_strings = []
                num_calls = 0
                # Find and annotate method calls.
                for version in method.versions:
                    for key in version.arg_types:
                        if key == "(self)":
                            continue
                        if key in def_arg_types:
                            def_arg_types[key].update(version.arg_types[key])
                        else:
                            def_arg_types[key] = set(version.arg_types[key])
                    for eval_code in version.eval_code:
                        def_eval_strings.append("eval(%s)" % eval_code)
                    if len(version.called_from) == 0:
                        continue
                    for caller in version.called_from:
                        call_filename = caller.callsite.method.source.file
                        # For now, if a method was called in the currently
                        # open file, but defined elsewhere, we ignore it.
                        if (call_filename != src_file_name or
                              method.source.file != src_file_name or
                              method.source.line_start < 0):
                            continue
                        call_lineno = caller.callsite.line
                        if method.is_mega:
                            call_msg = ("Call to %s (megamorphic) defined on line %d." %
                                        (method.name, def_lineno))
                        else:
                            call_msg = ("Call to %s defined on line %d." %
                                        (method.name, def_lineno))
                        call_msg += "\n(line numbers may be inaccurate in polyglot programs)"
                        self._annotate_text(call_lineno, call_filename,
                                            method.name, call_msg,
                                            JRubyMorphismMsg)
                        self._annotate_text(call_lineno, call_filename,
                                            method.name,
                                            { method.name : method.is_mega },
                                            JRubyCallGraph)
                        num_calls += 1
                # We can't visualise definitions which are never called.
                if num_calls > 0:
                    def_msg += " and is called %d times." % num_calls
                    self._annotate_text(def_lineno, def_filename,
                                        method.name, def_msg, JRubyMorphismMsg)
                    self._annotate_text(def_lineno, def_filename,
                                        method.name,
                                        { method.name : method.is_mega },
                                        JRubyCallGraph)
                    # Eval strings interpreted at runtime.
                    if def_eval_strings:
                        self._annotate_text(def_lineno, def_filename,
                                            method.name, ("%s" %
                                                 ", ".join(def_eval_strings)),
                                            JRubyEvalStrings)
                    # Argument types used at runtime
                    def_types_list = []
                    for name in sorted(def_arg_types.keys()):
                        types = ", ".join(def_arg_types[name])
                        def_types_list.append("%s in {%s}" % (name, types))
                    if def_arg_types:
                        self._annotate_text(def_lineno, def_filename,
                                            method.name,
                                            ", ".join(def_types_list),
                                            JRubyArgumentTypes)

    def _annotate_text(self, lineno, filename, text, annotation, klass):
        """Annotate a node on a given line with a given symbol name."""
        if lineno < 0:  # Method not defined by the programmer (e.g. <main>)
            return
        # Attempt to find 'text' on 'lineno'.
        try:
            temp_cursor = self.tm.cursor.copy()
            temp_cursor.line = lineno - 2
            temp_cursor.move_to_x(0)
            node = temp_cursor.find_next_visible(temp_cursor.node)
            while node.lookup == "<ws>" or node.symbol.name != text:
                node = node.next_term
                if isinstance(node, EOS) or node is None:
                    raise ValueError("EOS")
            if node is not None:
                node.add_annotation(klass(annotation))
        except (ValueError, IndexError):
            try:
                # Could not find 'text' on 'lineno', so search the whole tree.
                # This is necessary because polyglot code will add wrappers to
                # the original program, and change the line numbers.
                temp_cursor = self.tm.cursor.copy()
                temp_cursor.line = 0
                temp_cursor.move_to_x(0)
                node = temp_cursor.find_next_visible(temp_cursor.node)
                while True:
                    if (node.symbol.name == text and
                       not node.has_annotation_by_class(klass)):
                        node.add_annotation(klass(annotation))
                        break
                    elif isinstance(node, EOS):
                        lbnode = self.tm.get_languagebox(node)
                        if lbnode:
                            node = lbnode
                        else:
                            break
                    node = node.next_term
            except Exception:
                logging.error("Failed to annotate '%s' on line %g" % \
                              (text, lineno))
