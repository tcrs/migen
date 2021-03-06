import os
import shutil

from migen.fhdl.structure import Signal
from migen.genlib.record import Record
from migen.genlib.io import CRG
from migen.fhdl import verilog, edif
from migen.build import tools


class ConstraintError(Exception):
    pass


class Pins:
    def __init__(self, *identifiers):
        self.identifiers = []
        for i in identifiers:
            self.identifiers += i.split()

    def __repr__(self):
        return "{}('{}')".format(self.__class__.__name__,
                                 " ".join(self.identifiers))


class IOStandard:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return "{}('{}')".format(self.__class__.__name__, self.name)


class Drive:
    def __init__(self, strength):
        self.strength = strength

    def __repr__(self):
        return "{}('{}')".format(self.__class__.__name__, self.strength)


class Misc:
    def __init__(self, misc):
        self.misc = misc

    def __repr__(self):
        return "{}({})".format(self.__class__.__name__, repr(self.misc))


class Subsignal:
    def __init__(self, name, *constraints):
        self.name = name
        self.constraints = list(constraints)

    def __repr__(self):
        return "{}('{}', {})".format(
            self.__class__.__name__,
            self.name,
            ", ".join([repr(constr) for constr in self.constraints]))


class PlatformInfo:
    def __init__(self, info):
        self.info = info

    def __repr__(self):
        return "{}({})".format(self.__class__.__name__, repr(self.info))


def _lookup(description, name, number):
    for resource in description:
        if resource[0] == name and (number is None or resource[1] == number):
            return resource
    raise ConstraintError("Resource not found: {}:{}".format(name, number))


def _resource_type(resource):
    t = None
    for element in resource[2:]:
        if isinstance(element, Pins):
            assert(t is None)
            t = len(element.identifiers)
        elif isinstance(element, Subsignal):
            if t is None:
                t = []

            assert(isinstance(t, list))
            n_bits = None
            for c in element.constraints:
                if isinstance(c, Pins):
                    assert(n_bits is None)
                    n_bits = len(c.identifiers)

            t.append((element.name, n_bits))

    return t


class ConnectorManager:
    def __init__(self):
        self.connector_table = dict()

    def add_connectors(self, connectors):
        for connector in connectors:
            cit = iter(connector)
            conn_name = next(cit)
            if isinstance(connector[1], str):
                pin_list = []
                for pins in cit:
                    pin_list += pins.split()
                pin_list = [None if pin == "None" else pin for pin in pin_list]
            elif isinstance(connector[1], dict):
                pin_list = connector[1]
            else:
                raise ValueError("Unsupported pin list type {} for connector"
                                 " {}".format(type(connector[1]), conn_name))
            if conn_name in self.connector_table:
                raise ValueError(
                    "Connector specified more than once: {}".format(conn_name))

            self.connector_table[conn_name] = pin_list

    def resolve_identifier(self, identifier):
        if ":" in identifier:
            conn, pn = identifier.split(":")
            if pn.isdigit():
                pn = int(pn)
            return self.resolve_identifier(self.connector_table[conn][pn])
        else:
            return identifier

    def resolve_identifiers(self, identifiers):
        return [self.resolve_identifier(identifier)
                for identifier in identifiers]


def _separate_pins(constraints):
    pins = None
    others = []
    for c in constraints:
        if isinstance(c, Pins):
            assert(pins is None)
            pins = c.identifiers
        else:
            others.append(c)

    return pins, others


class ConstraintManager:
    def __init__(self, io, connectors):
        self.available = list(io)
        self.matched = []
        self.platform_commands = []
        self.connector_manager = ConnectorManager()
        self.connector_manager.add_connectors(connectors)

    def add_extension(self, io):
        self.available.extend(io)

    def add_connectors(self, connectors):
        self.connector_manager.add_connectors(connectors)

    def request(self, name, number=None):
        resource = _lookup(self.available, name, number)
        rt = _resource_type(resource)
        if isinstance(rt, int):
            obj = Signal(rt, name_override=resource[0])
        else:
            obj = Record(rt, name=resource[0])

        for element in resource[2:]:
            if isinstance(element, PlatformInfo):
                obj.platform_info = element.info
                break

        self.available.remove(resource)
        self.matched.append((resource, obj))
        return obj

    def lookup_request(self, name, number=None):
        for resource, obj in self.matched:
            if resource[0] == name and (number is None or
                                        resource[1] == number):
                return obj

        raise ConstraintError("Resource not found: {}:{}".format(name, number))

    def add_platform_command(self, command, **signals):
        self.platform_commands.append((command, signals))

    def get_io_signals(self):
        r = set()
        for resource, obj in self.matched:
            if isinstance(obj, Signal):
                r.add(obj)
            else:
                r.update(obj.flatten())

        return r

    def get_sig_constraints(self):
        r = []
        for resource, obj in self.matched:
            name = resource[0]
            number = resource[1]
            has_subsignals = False
            top_constraints = []
            for element in resource[2:]:
                if isinstance(element, Subsignal):
                    has_subsignals = True
                else:
                    top_constraints.append(element)

            if has_subsignals:
                for element in resource[2:]:
                    if isinstance(element, Subsignal):
                        sig = getattr(obj, element.name)
                        pins, others = _separate_pins(top_constraints +
                                                      element.constraints)
                        pins = self.connector_manager.resolve_identifiers(pins)
                        r.append((sig, pins, others,
                                  (name, number, element.name)))
            else:
                pins, others = _separate_pins(top_constraints)
                pins = self.connector_manager.resolve_identifiers(pins)
                r.append((obj, pins, others, (name, number, None)))

        return r

    def get_platform_commands(self):
        return self.platform_commands


class GenericPlatform:
    def __init__(self, device, io, connectors=[], name=None):
        self.device = device
        self.constraint_manager = ConstraintManager(io, connectors)
        if name is None:
            name = self.__module__.split(".")[-1]
        self.name = name
        self.sources = set()
        self.finalized = False

    def request(self, *args, **kwargs):
        return self.constraint_manager.request(*args, **kwargs)

    def lookup_request(self, *args, **kwargs):
        return self.constraint_manager.lookup_request(*args, **kwargs)

    def add_period_constraint(self, clk, period):
        raise NotImplementedError

    def add_false_path_constraint(self, from_, to):
        raise NotImplementedError

    def add_false_path_constraints(self, *clk):
        for a in clk:
            for b in clk:
                if a is not b:
                    self.add_false_path_constraint(a, b)

    def add_platform_command(self, *args, **kwargs):
        return self.constraint_manager.add_platform_command(*args, **kwargs)

    def add_extension(self, *args, **kwargs):
        return self.constraint_manager.add_extension(*args, **kwargs)

    def add_connectors(self, *args, **kwargs):
        return self.constraint_manager.add_connectors(*args, **kwargs)

    def finalize(self, fragment, *args, **kwargs):
        if self.finalized:
            raise ConstraintError("Already finalized")
        # if none exists, create a default clock domain and drive it
        if not fragment.clock_domains:
            if not hasattr(self, "default_clk_name"):
                raise NotImplementedError(
                    "No default clock and no clock domain defined")
            crg = CRG(self.request(self.default_clk_name))
            fragment += crg.get_fragment()

        self.do_finalize(fragment, *args, **kwargs)
        self.finalized = True

    def do_finalize(self, fragment, *args, **kwargs):
        """overload this and e.g. add_platform_command()'s after the modules
        had their say"""
        if hasattr(self, "default_clk_period"):
            try:
                self.add_period_constraint(
                    self.lookup_request(self.default_clk_name),
                    self.default_clk_period)
            except ConstraintError:
                pass

    def add_source(self, filename, language=None, library=None):
        if language is None:
            language = tools.language_by_filename(filename)
        if language is None:
            language = "verilog"

        if library is None:
            library = "work"

        self.sources.add((os.path.abspath(filename), language, library))

    def add_source_dir(self, path, recursive=True, library=None):
        dir_files = []
        if recursive:
            for root, dirs, files in os.walk(path):
                for filename in files:
                    dir_files.append(os.path.join(root, filename))
        else:
            for item in os.listdir(path):
                if os.path.isfile(os.path.join(path, item)):
                    dir_files.append(os.path.join(path, item))
        for filename in dir_files:
            language = tools.language_by_filename(filename)
            if language is not None:
                self.add_source(filename, language, library)

    # copy all source files to the build_dir to make them
    # self-contained. returns a new set.
    def copy_sources(self, build_dir, subdir="imports"):
        copied_sources = set()

        for filename, language, library in self.sources:
            path = _make_local_path(subdir, filename)

            # source filenames are assumed relative to the build_dir
            src = os.path.join(build_dir, filename)
            # copy to path that starts with build_dir
            dest = os.path.join(build_dir, path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(src, dest)

            # return entries relative to build_dir
            copied_sources.add((path, language, library))

        return copied_sources

    def resolve_signals(self, vns):
        # resolve signal names in constraints
        sc = self.constraint_manager.get_sig_constraints()
        named_sc = [(vns.get_name(sig), pins, others, resource)
                    for sig, pins, others, resource in sc]
        # resolve signal names in platform commands
        pc = self.constraint_manager.get_platform_commands()
        named_pc = []
        for template, args in pc:
            name_dict = dict((k, vns.get_name(sig)) for k, sig in args.items())
            named_pc.append(template.format(**name_dict))

        return named_sc, named_pc

    def get_verilog(self, fragment, **kwargs):
        return verilog.convert(
            fragment,
            self.constraint_manager.get_io_signals(),
            create_clock_domains=False, **kwargs)

    def get_edif(self, fragment, cell_library, vendor, device, **kwargs):
        return edif.convert(
            fragment,
            self.constraint_manager.get_io_signals(),
            cell_library, vendor, device, **kwargs)

    def build(self, fragment):
        raise NotImplementedError("GenericPlatform.build must be overloaded")

    def create_programmer(self):
        raise NotImplementedError

# makes potentially absolute `path` local to `subdir`, possibly
# removing a python path to improve legibility
def _make_local_path(subdir, path):
    path_parts = []
    while True:
        (path, part) = os.path.split(path)
        if part == "": break
        path_parts.append(part)
    path_parts.reverse()

    try:
        idx = path_parts.index("site-packages")
        path_parts = path_parts[(idx + 1):]
    except ValueError:
        pass

    return os.path.join(subdir, *path_parts)
