#! /usr/bin/python
__copyright__ = "(C) 2016-2017 Guido U. Draheim, for free use (CC-BY, GPL, BSD)"
__version__ = "0.5.1128"

import logging
logg = logging.getLogger("systemctl")

import re
import fnmatch
import shlex
import collections
import ConfigParser
import errno
import os
import sys
import subprocess
import signal
import time


# http://stackoverflow.com/questions/568271/how-to-check-if-there-exists-a-process-with-a-given-pid
def pid_exists(pid):
    """Check whether pid exists in the current process table.
    UNIX only.
    """
    if pid is None:
        return False
    if pid < 0:
        return False
    if pid == 0:
        # According to "man 2 kill" PID 0 refers to every process
        # in the process group of the calling process.
        # On certain systems 0 is a valid PID but we have no way
        # to know that in a portable fashion.
        raise ValueError('invalid PID 0')
    try:
        os.kill(pid, 0)
    except OSError as err:
        if err.errno == errno.ESRCH:
            # ESRCH == No such process
            return False
        elif err.errno == errno.EPERM:
            # EPERM clearly means there's a process to deny access to
            return True
        else:
            # According to "man 2 kill" possible error values are
            # (EINVAL, EPERM, ESRCH)
            raise
    else:
        return True

def checkstatus(cmd):
    if cmd.startswith("-"):
        return False, cmd[1:]
    else:
        return True, cmd

# https://github.com/phusion/baseimage-docker/blob/rel-0.9.16/image/bin/my_init
def ignore_signals_and_raise_keyboard_interrupt(signame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    raise KeyboardInterrupt(signame)

class UnitConfigParser:
    """ A *.service files has a structure similar to an *.ini file but it is
        actually not like it. Settings may occur multiple times in each section
        and they create an implicit list. In reality all the settings are
        globally uniqute, so that an 'environment' can be printed without
        adding prefixes. Settings are continued with a backslash at the end
        of the line.  """
    def __init__(self, defaults=None, dict_type=None, allow_no_value=False):
        self._defaults = defaults or {}
        self._dict_type = dict_type or collections.OrderedDict
        self._allow_no_value = allow_no_value
        self._dict = self._dict_type()
        self._files = []
    def defaults(self):
        return self.defaults
    def sections(self):
        return self._dict.keys()
    def add_section(self, section):
        if section not in self._dict:
            self._dict[section] = self._dict_type()
    def has_section(self, section):
        return section in self._dict
    def has_option(self, section, option):
        if section in self._dict:
            return False
        return option in self._dict[section]
    def set(self, section, option, value):
        if section not in self._dict:
            self._dict[section] = self._dict_type()
        if option not in self._dict[section]:
            self._dict[section][option] = [ value ]
        else:
            self._dict[section][option].append(value)
        if not value:
            self._dict[section][option] = []
    def get(self, section, option, default = None, allow_no_value = False):
        allow_no_value = allow_no_value or self._allow_no_value
        if section not in self._dict:
            if default is not None:
                return default
            if allow_no_value:
                return None
            logg.error("section {} does not exist".format(section))
            logg.error("  have {}".format(self.sections()))
            raise AttributeError("section {} does not exist".format(section))
        if option not in self._dict[section]:
            if default is not None:
                return default
            if allow_no_value:
                return None
            raise AttributeError("option {} in {} does not exist".format(option, section))
        if not self._dict[section][option]:
            if default is not None:
                return default
            if allow_no_value:
                return None
        return self._dict[section][option][0]
    def getlist(self, section, option, default = None, allow_no_value = False):
        allow_no_value = allow_no_value or self._allow_no_value
        if section not in self._dict:
            if default is not None:
                return default
            if allow_no_value:
                return []
            logg.error("section {} does not exist".format(section))
            logg.error("  have {}".format(self.sections()))
            raise AttributeError("section {} does not exist".format(section))
        if option not in self._dict[section]:
            if default is not None:
                return default
            if allow_no_value:
                return None
            raise AttributeError("option {} in {} does not exist".format(option, section))
        return self._dict[section][option]
    def loaded(self):
        return len(self._files)
    def filename(self):
        """ returns the last filename that was parsed """
        if self._files:
            return self._files[-1]
        return None
    def read(self, filename):
        return self.read_sysd(filename)
    def read_sysd(self, filename):
        initscript = False
        initinfo = False
        section = None
        if os.path.isfile(filename):
            self._files.append(filename)
        nextline = False
        name, text = "", ""
        for orig_line in open(filename):
            if nextline:
                text += orig_line
                if text.rstrip().endswith("\\"):
                    text = text.rstrip() + "\n"
                else:
                    self.set(section, name, text)
                continue
            line = orig_line.strip()
            if not line:
                continue
            if line.startswith("#"):
                continue
            if line.startswith("["):
                x = line.find("]")
                if x > 0:
                    section = line[1:x]
                    self.add_section(section)
                continue
            m = re.match(r"(\w+)=(.*)", line)
            if not m:
                logg.warning("bad ini line: %s", line)
                raise Exception("bad ini line")
            name, text = m.group(1), m.group(2).strip()
            if text.endswith("\\"):
                nextline = True
                text = text + "\n"
            else:
                self.set(section, name, text)
    def read_sysv(self, filename):
        """ an LSB header is scanned and converted to (almost)
            equivalent settings of a SystemD ini-style input """
        initscript = False
        initinfo = False
        section = None
        if os.path.isfile(filename):
            self._files.append(filename)
        for orig_line in open(filename):
            line = orig_line.strip()
            if line.startswith("#"):
                if " BEGIN INIT INFO" in line: 
                     initinfo = True
                     section = "init.d"
                if " END INIT INFO" in line: 
                     initinfo = False
                if initinfo:
                    m = re.match(r"^\S+\s*(\w[\w_-]*):(.*)", line)
                    if m:
                        self.set(section, m.group(1), m.group(2).strip())
                continue
        description = self.get("init.d", "Description", "")
        self.set("Unit", "Description", description)
        check = self.get("init.d", "Required-Start","")
        for item in check.split(" "):
            if item.strip() == "$network":
                self.set("Unit", "After", "network.target")
            if item.strip() == "$remote_fs":
                self.set("Unit", "After", "remote-fs.target")
            if item.strip() == "$local_fs":
                self.set("Unit", "After", "local-fs.target")
            if item.strip() == "$timer":
                self.set("Unit", "Requires", "basic.target")
        provides = self.get("init.d", "Provides", "")
        if provides:
            self.set("Install", "Alias", provides)
        # if already in multi-user.target then start it there.
        runlevels = self.get("init.d", "Default-Start","")
        if "5" in runlevels:
            self.set("Install", "WantedBy", "graphical.target")
        if "3" in runlevels:
            self.set("Install", "WantedBy", "multi-user.target")
        self.set("Service", "Type", "sysv")

UnitParser = ConfigParser.RawConfigParser
UnitParser = UnitConfigParser

def subprocess_nowait(cmd, env=None):
    run = subprocess.Popen(cmd, shell=True, env=env)
    return run

def subprocess_wait(cmd, env=None, check = False):
    run = subprocess.Popen(cmd, shell=True, env=env)
    run.wait()
    if check and run.returncode: 
        logg.error("returncode %i\n %s", run.returncode, cmd)
        raise Exception("command failed")
    return run

def subprocess_output(cmd, env=None, check = False):
    run = subprocess.Popen(cmd, shell=True, env=env, stdout = subprocess.PIPE)
    run.wait()
    if check and run.returncode: 
        logg.error("returncode %i\n %s", run.returncode, cmd)
        raise Exception("command failed")
    return run

_sysd_default = "multi-user.target"
_sysd_folder1 = "/usr/lib/systemd/system"
_sysd_folder2 = "/etc/systemd/system"
_sysv_folder1 = "/etc/init.d"
_sysv_folder2 = "/var/run/init.d"
_waitprocfile = 100
_waitkillproc = 10
_force = False
_quiet = False
_full = False
_property = None

class Systemctl:
    def __init__(self):
        self._sysd_folder1 = _sysd_folder1
        self._sysd_folder2 = _sysd_folder2
        self._sysv_folder1 = _sysv_folder1
        self._sysv_folder2 = _sysv_folder2
        self._waitprocfile = _waitprocfile
        self._waitkillproc = _waitkillproc
        self._force = _force
        self._quiet = _quiet
        self._full = _full
        self._loaded_file_sysv = {} # /etc/init.d/name => config data
        self._loaded_file_sysd = {} # /etc/systemd/system/name.service => config data
        self._file_for_unit_sysv = None # name.service => /etc/init.d/name
        self._file_for_unit_sysd = None # name.service => /etc/systemd/system/name.service
    def unit_file(self, module = None): # -> filename?
        """ file path for the given module (sysv or systemd) """
        path = self.unit_sysd_file(module)
        if path is not None: return path
        path = self.unit_sysv_file(module)
        if path is not None: return path
        return None
    def scan_unit_sysd_files(self, module = None): # -> [ unit-names,... ]
        """ reads all unit files, returns the last filename for the unit given """
        if self._file_for_unit_sysd is None:
            self._file_for_unit_sysd = {}
            for folder in (self._sysd_folder1, self._sysd_folder2):
                if not os.path.isdir(folder):
                    continue
                for name in os.listdir(folder):
                    path = os.path.join(folder, name)
                    self._file_for_unit_sysd[name] = path
            logg.debug("found %s sysd files", len(self._file_for_unit_sysd))
        return self._file_for_unit_sysd.keys()
    def unit_sysd_file(self, module = None): # -> filename?
        """ file path for the given module (systemd) """
        self.scan_unit_sysd_files()
        if module and module in self._file_for_unit_sysd:
            return self._file_for_unit_sysd[module]
        if module and module+".service" in self._file_for_unit_sysd:
            return self._file_for_unit_sysd[module+".service"]
        return None
    def scan_unit_sysv_files(self, module = None): # -> [ unit-names,... ]
        """ reads all init.d files, returns the last filename when unit is a '.service' """
        if self._file_for_unit_sysv is None:
            self._file_for_unit_sysv = {}
            for folder in (self._sysv_folder1, self._sysv_folder2):
                if not os.path.isdir(folder):
                    continue
                for name in os.listdir(folder):
                    path = os.path.join(folder, name)
                    self._file_for_unit_sysv[name+".service"] = path
            logg.debug("found %s sysv files", len(self._file_for_unit_sysv))
        return self._file_for_unit_sysv.keys()
    def unit_sysv_file(self, module = None): # -> filename?
        """ file path for the given module (sysv) """
        self.scan_unit_sysv_files()
        if module and module in self._file_for_unit_sysv:
            return self._file_for_unit_sysv[module]
        if module and module+".service" in self._file_for_unit_sysv:
            return self._file_for_unit_sysv[module+".service"]
        return None
    def is_sysv_unit(self, module): # -> bool?
        """ for routines that have a special treatment for init.d services """
        self.unit_file() # scan all
        if not filename: return None
        if module in self._file_for_unit_sysd: return False
        if module in self._file_for_unit_sysv: return True
        return None # not True
    def is_sysv_file(self, filename):
        """ for routines that have a special treatment for init.d services """
        self.unit_file() # scan all
        if not filename: return None
        if filename in self._file_for_unit_sysd.values(): return False
        if filename in self._file_for_unit_sysv.values(): return True
        return None # not True
    def read_unit(self, module): # -> conf | not-found
        """ read the unit file with a UnitParser (sysv or systemd) """
        data = self.read_sysd_unit(module)
        if data is not None: 
            return data
        data = self.read_sysv_unit(module)
        if data is not None: 
            return data
        logg.warning("unit file not found: %s", module)
        raise Exception("unit file not found")
    def read_sysd_unit(self, module): # -> conf?
        """ read the unit file with a UnitParser (systemd) """
        path = self.unit_sysd_file(module)
        if not path: return None
        return self.read_sysd_file(path)
    def read_sysd_file(self, path): # -> conf?
        """ read the unit file with a UnitParser (systemd) """
        if path is None: return None
        if path in self._loaded_file_sysd:
            return self._loaded_file_sysd[path]
        unit = UnitParser()
        unit.read_sysd(path)
        override_d = path + ".d"
        if os.path.isdir(override_d):
            for name in os.listdir(override_d):
                if name.endswith(".conf"):
                    unit.read_sysd(os.path.join(override_d, name))
        self._loaded_file_sysd[path] = unit
        return unit
    def read_sysv_unit(self, module): # -> conf?
        """ read the unit file with a UnitParser (sysv) """
        path = self.unit_sysv_file(module)
        if not path: return None
        return self.read_sysv_file(path)
    def read_sysv_file(self, path): # -> conf?
        """ read the unit file with a UnitParser (sysv) """
        if path is None: return None
        if path in self._loaded_file_sysv:
            return self._loaded_file_sysv[path]
        unit = UnitParser()
        unit.read_sysv(path)
        self._loaded_file_sysv[path] = unit
        return unit
    def default_unit(self, module): # -> conf
        """ a unit conf that can be printed to the user where
            attributes are empty and loaded() is False """
        conf = UnitParser()
        conf.set("Unit","Id", module)
        conf.set("Unit", "Names", module)
        conf.set("Unit", "Description", "NOT-FOUND "+module)
        return conf
    def try_read_unit(self, module): # -> conf (conf | default-conf)
        """ accept that a unit does not exist 
            and return a unit conf that says 'not-loaded' """
        try: 
            return self.read_unit(module)
        except Exception, e: 
            logg.debug("read unit '%s': %s", module, e)
            return self.default_unit(module)
    def match_units(self, modules, suffix=".service"): # -> [ units,.. ]
        """ call for about any command with multiple units which can
            actually be glob patterns on their respective filename. """
        found = []
        for unit in self.match_sysd_units(modules, suffix):
            if unit not in found:
                found.append(unit)
        for unit in self.match_sysv_units(modules, suffix):
            if unit not in found:
                found.append(unit)
        return found
    def match_sysd_units(self, modules, suffix=".service"): # -> generate[ unit ]
        """ make a file glob on all known units (systemd areas) """
        if isinstance(modules, basestring):
            modules = [ modules ]
        self.scan_unit_sysd_files()
        for item in sorted(self._file_for_unit_sysd.keys()):
            if not modules:
                yield item
            elif [ module for module in modules if fnmatch.fnmatchcase(item, module) ]:
                yield item
            elif [ module for module in modules if module+suffix == item ]:
                yield item
    def match_sysv_units(self, modules, suffix=".service"): # -> generate[ unit ]
        """ make a file glob on all known units (sysv areas) """
        if isinstance(modules, basestring):
            modules = [ modules ]
        self.scan_unit_sysv_files()
        for item in sorted(self._file_for_unit_sysv.keys()):
            if not modules:
                yield item
            elif [ module for module in modules if fnmatch.fnmatchcase(item, module) ]:
                yield item
            elif [ module for module in modules if module+suffix == item ]:
                yield item
    def system_list_services(self):
        """ show all the services """
        filename = self.unit_file() # scan all
        result = ""
        for name, value in self._file_for_unit_sysd.items():
            result += "\nSysD {name} = {value}".format(**locals())
        for name, value in self._file_for_unit_sysv.items():
            result += "\nSysV {name} = {value}".format(**locals())
        return result
    def show_list_units(self, *modules): # -> [ (unit,loaded,description) ]
        """ show all the units """
        result = {}
        description = {}
        for unit in self.match_units(modules):
            result[unit] = None
            description[unit] = ""
            try: 
                conf = self.try_read_unit(unit)
                result[unit] = conf
                description[unit] = self.get_description_from(conf)
            except Exception, e:
                logg.warning("list-units: %s", e)
        return [ (unit, result[unit] and "loaded" or "", description[unit]) for unit in sorted(result) ]
    def get_description_from(self, conf, default = None): # -> text
        """ Unit.Description could be empty sometimes """
        if not conf: return default or ""
        return conf.get("Unit", "Description", default or "")
    def write_pid_file(self, pid_file, pid): # -> bool(written)
        """ if a pid_file is known then path is created and the
            give pid is written as the only content. """
        if not pid_file: 
            logg.debug("pid %s but no pid_file", pid)
            return False
        dirpath = os.path.dirname(os.path.abspath(pid_file))
        if not os.path.isdir(dirpath):
            os.makedirs(dirpath)
        with open(pid_file, "w") as f:
            f.write("{}\n".format(pid))
        return True
    def pid_exists(self, pid): # -> bool
        """ check if a pid does still exist (unix standard) """
        # return os.path.isdir("/proc/%s" % pid) # (linux standard) 
        return pid_exists(pid)
    def wait_pid_file(self, pid_file): # -> pid?
        """ wait some seconds for the pid file to appear and return the pid """
        dirpath = os.path.dirname(os.path.abspath(pid_file))
        for x in xrange(self._waitprocfile):
            if not os.path.isdir(dirpath):
                self.sleep(1)
                continue
            pid = self.read_pid_file(pid_file)
            if not pid:
                continue
            if not pid_exists(pid):
                continue
            return pid
        return None
    def default_pid_file(self, unit): # -> text
        """ default file pattern where to store a pid """
        return "/var/run/%s.pid" % unit
    def read_env_file(self, env_file): # -> generate[ (name,value) ]
        """ EnvironmentFile=<name> is being scanned """
        if env_file.startswith("-"):
            env_file = env_file[1:]
            if not os.path.isfile(env_file):
                return
        try:
            for real_line in open(env_file):
                line = real_line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"([\w_]+)[=]'([^']*)'", line)
                if m:
                    yield m.group(1), m.group(2)
                    continue
                m = re.match(r'([\w_]+)[=]"([^"]*)"', line)
                if m:
                    yield m.group(1), m.group(2)
                    continue
                m = re.match(r'([\w_]+)[=](.*)', line)
                if m:
                    yield m.group(1), m.group(2)
                    continue
        except Exception, e:
            logg.info("while reading %s: %s", env_file, e)
    def read_env_part(self, env_part): # -> generate[ (name, value) ]
        """ Environment=<name>=<value> is being scanned """
        try:
            for real_line in env_part.split("\n"):
                line = real_line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.match(r"([\w_]+)[=]'([^']*)'", line)
                if m:
                    yield m.group(1), m.group(2)
                    continue
                m = re.match(r'([\w_]+)[=]"([^"]*)"', line)
                if m:
                    yield m.group(1), m.group(2)
                    continue
                m = re.match(r'([\w_]+)[=](.*)', line)
                if m:
                    yield m.group(1), m.group(2)
                    continue
        except Exception, e:
            logg.info("while reading %s: %s", env_part, e)
    def sleep(self, seconds = None): 
        """ just sleep """
        seconds = seconds or 1
        time.sleep(seconds)
    def sudo_from(self, conf):
        """ calls runuser with a (non-priviledged) user """
        runuser = conf.get("Service", "User", "")
        rungroup = conf.get("Service", "Group", "")
        sudo = ""
        if runuser and rungroup:
            sudo = "/usr/sbin/runuser -g %s -u %s -- " % (rungroup, runuser)
        elif runuser:
            sudo = "/usr/sbin/runuser -u %s -- " % (runuser)
        elif rungroup:
            sudo = "/usr/sbin/runuser -g %s -- " % (rungroup)
        return sudo
    def start_of_units(self, *modules):
        done = True
        for unit in self.match_units(modules):
            if not self.start_unit(unit):
                done = False
        return done
    def start_unit(self, unit):
        conf = self.read_unit(unit)
        return self.start_unit_from(conf)
    def start_unit_from(self, conf):
        if not conf: return
        runs = conf.get("Service", "Type", "simple").lower()
        sudo = self.sudo_from(conf)
        env = self.get_env(conf)
        logg.info("env = %s", env)
        if True:
            for cmd in conf.getlist("Service", "ExecStartPre", []):
                check, cmd = checkstatus(cmd)
                logg.info("ExecStartPre:%s:%s", check, cmd)
                subprocess_wait(cmd, env, check=check)
        if runs in [ "sysv" ]:
            if True:
                 exe = conf.filename()
                 cmd = "'%s' start" % exe
                 env["SYSTEMCTL_SKIP_REDIRECT"] = "yes"
                 logg.info("(start) %s", cmd)
                 run = subprocess_wait(cmd, env)
        elif runs in [ "simple", "oneshot", "notify" ]: 
            for cmd in conf.getlist("Service", "ExecStart", []):
                 pid_file = self.get_pid_file_from(conf)
                 pid = self.read_pid_file(pid_file, "")
                 env["MAINPID"] = str(pid)
                 logg.info("[start] %s", sudo+cmd)
                 run = subprocess_nowait(sudo+cmd, env)
                 self.write_pid_file(pid_file, run.pid)
                 if runs in [ "oneshot" ]: run.wait()
        elif runs in [ "forking" ]:
            for cmd in conf.getlist("Service", "ExecStart", []):
                 check, cmd = checkstatus(cmd)
                 logg.info("{start} %s", sudo+cmd)
                 run = subprocess_wait(sudo+cmd, env)
                 if check and run.returncode: raise Exception("ExecStart")
                 pid_file = self.get_pid_file_from(conf)
                 self.wait_pid_file(pid_file)
        else:
            logg.error("unsupported run type '%s'", runs)
            raise Exception("unsupported run type")
        if True:
            for cmd in conf.getlist("Service", "ExecStartPost", []):
                check, cmd = checkstatus(cmd)
                logg.info("ExecStartPost:%s:%s", check, cmd)
                subprocess_wait(cmd, env, check=check)
        return True
    def read_pid_file(self, pid_file, default = None):
        pid = default
        if not pid_file:
            return default
        if not os.path.isfile(pid_file):
            return default
        try:
            for line in open(pid_file):
                if line.strip(): 
                    pid = int(line.strip())
                    break
        except:
            logg.warning("bad read of pid file '%s'", pid_file)
        return pid
    def kill_pid(self, pid):
        if not pid:
            return
        for x in xrange(self._waitkillproc):
            os.kill(pid, signal.SIGTERM)
            if not self.pid_exists(pid):
                break
            self.sleep(1)
            if not self.pid_exists(pid):
                break
        for x in xrange(self._waitkillproc):
            if not self.pid_exists(pid):
                break
            os.kill(pid, signal.SIGKILL)
            self.sleep(1)
    def environment_of_unit(self, unit):
        conf = self.read_unit(unit)
        return self.get_env(conf)
    def get_env(self, conf):
        env = os.environ.copy()
        for env_part in conf.getlist("Service", "Environment", []):
            for name, value in self.read_env_part(env_part):
                env[name] = value
        for env_file in conf.getlist("Service", "EnvironmentFile", []):
            for name, value in self.read_env_file(env_file):
                env[name] = value
        return env
    def stop_of_units(self, *modules):
        done = True
        for unit in self.match_units(modules):
            if not self.stop_unit(unit):
                done = False
        return done
    def stop_unit(self, unit):
        conf = self.read_unit(unit)
        return self.stop_unit_from(conf)
    def stop_unit_from(self, conf):
        if not conf: return
        runs = conf.get("Service", "Type", "simple").lower()
        sudo = self.sudo_from(conf)
        env = self.get_env(conf)
        if True:
            for cmd in conf.getlist("Service", "ExecStopPre", []):
                check, cmd = checkstatus(cmd)
                logg.info("ExecStopPre:%s:%s", check, cmd)
                subprocess_wait(cmd, env, check=check)
        if runs in [ "sysv" ]:
            if True:
                 exe = conf.filename()
                 cmd = "'%s' stop" % exe
                 env["SYSTEMCTL_SKIP_REDIRECT"] = "yes"
                 logg.info("(stop) %s", cmd)
                 run = subprocess_wait(cmd, env)
        elif not conf.getlist("Service", "ExecStop", []):
            if True:
                 pid_file = self.get_pid_file_from(conf)
                 pid = self.read_pid_file(pid_file)
                 logg.info("(stop) kill %s (%s)", pid, pid_file)
                 self.kill_pid(pid)
                 if os.path.isfile(pid_file):
                     os.remove(pid_file)
        elif runs in [ "simple", "oneshot", "notify" ]:
            for cmd in conf.getlist("Service", "ExecStop", []):
                 pid_file = self.get_pid_file_from(conf)
                 pid = self.read_pid_file(pid_file, "")
                 env["MAINPID"] = str(pid)
                 logg.info("[stop] %s", sudo+cmd)
                 run = subprocess_nowait(sudo+cmd, env)
                 # self.write_pid_file(pid_file, run.pid)
                 if runs in [ "oneshot" ]: run.wait()
        elif runs in [ "forking" ]:
            for cmd in conf.getlist("Service", "ExecStop", []):
                 active = self.is_active_from(conf)
                 pid_file = self.get_pid_file_from(conf)
                 pid = self.read_pid_file(pid_file, "")
                 env["MAINPID"] = str(pid)
                 check, cmd = checkstatus(cmd)
                 logg.info(" {env} %s", env)
                 logg.info("{stop} %s", sudo+cmd)
                 run = subprocess_wait(sudo+cmd, env)
                 if active:
                     if check and run.returncode: raise Exception("ExecStop")
                 pid_file = self.get_pid_file_from(conf)
                 self.wait_pid_file(pid_file)
        else:
            logg.error("unsupported run type '%s'", runs)
            raise Exception("unsupported run type")
        if True:
            for cmd in conf.getlist("Service", "ExecStopPost", []):
                check, cmd = checkstatus(cmd)
                logg.info("ExecStopPost:%s:%s", check, cmd)
                subprocess_wait(cmd, env, check=check)
        return True
    def reload_of_units(self, *modules):
        done = True
        for unit in self.match_units(modules):
            if not self.reload_unit(unit):
                done = False
        return done
    def reload_unit(self, unit):
        conf = self.read_unit(unit)
        return self.reload_unit_from(conf)
    def reload_unit_from(self, conf):
        if not conf: return
        runs = conf.get("Service", "Type", "simple").lower()
        sudo = self.sudo_from(conf)
        env = self.get_env(conf)
        if True:
            for cmd in conf.getlist("Service", "ExecReloadPre", []):
                check, cmd = checkstatus(cmd)
                logg.info("ExecReloadPre:%s:%s", check, cmd)
                subprocess_wait(cmd, env, check=check)
        if runs in [ "sysv" ]:
            if True:
                 exe = conf.filename()
                 cmd = "'%s' reload" % exe
                 env["SYSTEMCTL_SKIP_REDIRECT"] = "yes"
                 logg.info("(reload) %s", cmd)
                 run = subprocess_wait(cmd, env)
        elif runs in [ "simple", "oneshot", "notify" ]:
            for cmd in conf.getlist("Service", "ExecReload", []):
                 pid_file = self.get_pid_file_from(conf)
                 pid = self.read_pid_file(pid_file, "")
                 env["MAINPID"] = str(pid)
                 logg.info("[reload] %s", sudo+cmd)
                 run = subprocess_nowait(sudo+cmd, env)
                 # self.write_pid_file(pid_file, run.pid)
                 if runs in [ "oneshot" ]: run.wait()
        elif runs in [ "forking" ]:
            for cmd in conf.getlist("Service", "ExecReload", []):
                 pid_file = self.get_pid_file_from(conf)
                 pid = self.read_pid_file(pid_file, "")
                 env["MAINPID"] = str(pid)
                 check, cmd = checkstatus(cmd)
                 logg.info("{reload} %s", sudo+cmd)
                 run = subprocess_nowait(sudo+cmd, env)
                 if check and run.returncode: raise Exception("ExecReload")
                 pid_file = self.get_pid_file_from(conf)
                 self.wait_pid_file(pid_file)
        else:
            logg.error("unsupported run type '%s'", runs)
            raise Exception("unsupported run type")
        if True:
            for cmd in conf.getlist("Service", "ExecReloadPost", []):
                check, cmd = checkstatus(cmd)
                logg.info("ExecReloadPost:%s:%s", check, cmd)
                subprocess_wait(cmd, env, check=check)
        return True
    def restart_of_units(self, *modules):
        done = True
        for unit in self.match_units(modules):
            if not self.restart_unit(unit):
                 done = False
        return done
    def restart_unit(self, unit):
        conf = self.read_unit(unit)
        return self.restart_unit_from(conf)
    def restart_unit_from(self, conf):
        if not conf: return
        runs = conf.get("Service", "Type", "simple").lower()
        sudo = self.sudo_from(conf)
        env = self.get_env(conf)
        if True:
            for cmd in conf.getlist("Service", "ExecRestartPre", []):
                check, cmd = checkstatus(cmd)
                logg.info("ExecRestartPre:%s:%s", check, cmd)
                subprocess_wait(cmd, env, check=check)
        if runs in [ "sysv" ]:
            if True:
                 exe = conf.filename()
                 cmd = "'%s' restart" % exe
                 env["SYSTEMCTL_SKIP_REDIRECT"] = "yes"
                 logg.info("(restart) %s", cmd)
                 run = subprocess_wait(cmd, env)
        elif not conf.getlist("Service", "ExceRestart", []):
            logg.info("(restart) => stop/start")
            self.stop_unit_from(conf)
            self.start_unit_from(conf)
        elif runs in [ "simple", "oneshot", "notify" ]:
            for cmd in conf.getlist("Service", "ExecRestart", []):
                 pid_file = self.get_pid_file_from(conf)
                 pid = self.read_pid_file(pid_file, "")
                 env["MAINPID"] = str(pid)
                 logg.info("[restart] %s", sudo+cmd)
                 run = subprocess_nowait(sudo+cmd, env)
                 # self.write_pid_file(pid_file, run.pid)
                 if runs in [ "oneshot" ]: run.wait()
        elif runs in [ "forking" ]:
            for cmd in conf.getlist("Service", "ExecRestart", []):
                 check, cmd = checkstatus(cmd)
                 logg.info("{restart} %s", sudo+cmd)
                 run = subprocess_wait(sudo+cmd, env)
                 if check and run.returncode: raise Exception("ExecRestart")
                 pid_file = self.get_pid_file_from(conf)
                 self.wait_pid_file(pid_file)
        else:
            logg.error("unsupported run type '%s'", runs)
            raise Exception("unsupported run type")
        if True:
            for cmd in conf.getlist("Service", "ExecRestartPost", []):
                check, cmd = checkstatus(cmd)
                logg.info("ExecRestartPost:%s:%s", check, cmd)
                subprocess_wait(cmd, env, check=check)
        return True
    def get_pid_file(self, unit):
        conf = self.read_unit(unit)
        return self.get_pid_file_from(conf)
    def get_pid_file_from(self, conf, default = None):
        if not conf: return default
        if not conf.filename(): return default
        unit = os.path.basename(conf.filename())
        if default is None:
            default = self.default_pid_file(unit)
        return conf.get("Service", "PIDFile", default)
    def try_restart_of_units(self, *modules):
        done = True
        for unit in self.match_units(modules):
            if not self.try_restart(unit):
                done = False
        return done
    def try_restart(unit):
        conf = self.read_unit(unit)
        if self.is_active_from(conf):
            return self.restart_unit_from(conf)
        return True
    def reload_or_restart_of_units(self, *modules):
        done = True
        for unit in self.match_units(modules):
            if not self.reload_or_start(unit):
                done = False
        return done
    def reload_or_restart(self, unit):
        conf = self.read_unit(unit)
        if not self.is_active_from(conf):
            # try: self.stop_unit_from(conf)
            # except Exception, e: pass
            return self.start_unit_from(conf)
        elif conf.getlist("Service", "ExecReload", []):
            return self.reload_unit_from(conf)
        else:
            return self.restart_unit_from(conf)
    def reload_or_try_restart_of_units(self, *modules):
        done = True
        for unit in self.match_units(modules):
            if not self.reload_or_try_restart(unit):
                done = False
        return done
    def reload_or_try_restart(unit):
        conf = self.read_unit(unit)
        if conf.getlist("Service", "ExecReload", []):
            return self.reload_unit_from(conf)
        elif not self.is_active_from(conf):
            return True
        else:
            return self.restart_unit_from(conf)
    def kill_of_units(self, *modules):
        units = {}
        for unit in self.match_units(modules):
            units[unit] = 1
        for unit in units:
            self.kill_unit(unit)
    def kill_unit(self, unit):
        conf = self.read_unit(unit)
        self.kill_unit_from(conf)
    def kill_unit_from(self, conf):
        if not conf: return
        pid_file = self.get_pid_file_from(conf)
        pid = self.read_pid_file(pid_file)
        logg.debug("pid_file '%s' => PID %s", pid_file, pid)
        self.kill_pid(pid)
    def is_active_of_units(self, *modules):
        """ implements True if any is-active = True """
        units = {}
        for unit in self.match_units(modules):
            units[unit] = 1
        result = False
        for unit in units:
            if self.is_active(unit):
                result = True
        return result
    def is_active(self, unit):
        conf = self.try_read_unit(unit)
        if not conf.loaded():
            logg.warning("no such unit '%s'", unit)
        return self.is_active_from(conf)
    def active_pid_from(self, conf):
        if not conf: return False
        pid_file = self.get_pid_file_from(conf)
        pid = self.read_pid_file(pid_file)
        logg.debug("pid_file '%s' => PID %s", pid_file, pid)
        exists = self.pid_exists(pid)
        if not exists:
           return None
        return pid # string!!
    def is_active_from(self, conf):
        if not conf: return False
        if self.active_pid_from(conf) is None:
           return False
        return True
    def active_from(self, conf):
        if not conf: return False
        pid = self.active_pid_from(conf)
        if pid is None: return "dead"
        return "PID %s" % pid
    def is_failed_of_units(self, *modules):
        result = False
        for unit in self.match_units(modules):
            if self.is_failed(unit):
                result = True
        return result
    def is_failed(self, unit):
        conf = self.try_read_unit(unit)
        if not conf.loaded():
            logg.warning("no such unit '%s'", unit)
        return self.is_failed_from(conf)
    def is_failed_from(self, conf):
        if not conf: return True
        pid_file = self.get_pid_file_from(conf)
        pid = self.read_pid_file(pid_file)
        logg.debug("pid_file '%s' => PID %s", pid_file, pid)
        return not self.pid_exists(pid)
    def status_of_units(self, *modules):
        status, result = 0, ""
        for unit in self.match_units(modules):
            status1, result1 = self.status_unit(unit)
            if status1: status = status1
            if result: result += "\n\n"
            result += result1
        return status, result
    def status_unit(self, unit):
        conf = self.try_read_unit(unit)
        result = "%s - %s" % (unit, self.get_description_from(conf))
        if conf.loaded():
            result += "\n    Loaded: loaded ({}, {})".format(conf.filename(), self.enabled_from(conf) )
        else:
            result += "\n    Loaded: failed"
            return 3, result
        if self.is_active_from(conf):
            result += "\n    Active: active ({})".format(self.active_from(conf))
            return 0, result
        else:
            result += "\n    Active: inactive ({})".format(self.active_from(conf))
            return 3, result
    def cat_of_units(self, *modules):
        done = True
        for unit in self.match_units(modules):
            if not self.cat_unit(unit):
                done = False
        return done
    def cat_unit(self, unit):
        try:
            unit_file = self.unit_file(unit)
            return open(unit_file).read()
        except Exception, e:
            print "Unit {} is not-loaded: {}".format(unit, e)
            return False
    def wanted_from(self, conf, default = None):
        if not conf: return default
        return conf.get("Install", "WantedBy", default, True)
    def enablefolder(self, wanted = None):
        if not wanted: return None
        if not wanted.endswith(".wants"):
            wanted = wanted + ".wants"
        return "/etc/systemd/system/" + wanted
    def enable_of_units(self, *modules):
        done = True
        for unit in self.match_units(modules):
            if not self.enable_unit(unit):
                done = False
        return done
    def enable_unit(self, unit):
        unit_file = self.unit_file(unit)
        if self.is_sysv_file(unit_file):
            return self.enable_unit_sysv(unit_file)
        wanted = self.wanted_from(self.try_read_unit(unit))
        if not wanted: return False # wanted = "multi-user.target"
        folder = self.enablefolder(wanted)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        target = os.path.join(folder, os.path.basename(unit_file))
        if True:
            _f = self._force and "-f" or ""
            logg.info("ln -s {_f} '{unit_file}' '{target}'".format(**locals()))
        if self._force and os.path.islink(target):
            os.remove(target)
        if not os.path.islink(target):
            os.symlink(unit_file, target)
        return True
    def rc3_folder(self):
        if os.path.isdir("/etc/rc3.d"): return "/etc/rc3.d"
        return "/etc/init.d/rc3.d"
    def rc5_folder(self):
        if os.path.isdir("/etc/rc5.d"): return "/etc/rc5.d"
        return "/etc/init.d/rc5.d"
    def enable_unit_sysv(self, unit_file):
        # a "multi-user.target"/rc3 is also started in /rc5
        rc3 = self.enable_unit_sysv_folder(unit_file, self.rc3_folder())
        rc5 = self.enable_unit_sysv_folder(unit_file, self.rc5_folder())
        return rc3 and rc5
    def enable_unit_sysv_folder(self, unit_file, rc_folder):
        name = os.path.basename(unit_file)
        nameS = "S50"+name
        nameK = "K50"+name
        # do not double existing entries
        if found in os.listdir(rc_folder):
            m = re.match("S\d\d(.*)", found)
            if m and m.group(1) == name:
                nameS = found
            m = re.match("K\d\d(.*)", found)
            if m and m.group(1) == name:
                nameK = found
        target = os.path.join(rc_folder, nameS)
        if not os.path.exists(target):
            os.symlink(unit_file, target)
        target = os.path.join(rc_folder, nameK)
        if not os.path.exists(target):
            os.symlink(unit_file, target)
        return True
    def disable_of_units(self, *modules):
        done = True
        for unit in self.match_units(modules):
            if not self.disable_unit(unit):
               done = False
        return done
    def disable_unit(self, unit):
        unit_file = self.unit_file(unit)
        if self.is_sysv_file(unit_file):
            return self.disable_unit_sysv(unit_file)
        wanted = self.wanted_from(self.try_read_unit(unit))
        folder = self.enablefolder(wanted)
        if not os.path.isdir(folder):
            return False
        target = os.path.join(folder, os.path.basename(unit_file))
        if os.path.isfile(target):
            _f = self._force and "-f" or ""
            logg.info("rm {_f} '{target}'".format(**locals()))
            os.remove(target)
        return True
    def disable_unit_sysv(self, unit_file):
        rc3 = self.disable_unit_sysv_folder(unit_file, self.rc3_folder())
        rc5 = self.disable_unit_sysv_folder(unit_file, self.rc5_folder())
        return rc3 and rc5
    def disable_unit_sysv_folder(self, unit_file, rc_folder):
        # a "multi-user.target"/rc3 is also started in /rc5
        name = os.path.basename(unit_file)
        nameS = "S50"+name
        nameK = "K50"+name
        # do not forget the existing entries
        if found in os.listdir(rc_folder):
            m = re.match("S\d\d(.*)", found)
            if m and m.group(1) == name:
                nameS = found
            m = re.match("K\d\d(.*)", found)
            if m and m.group(1) == name:
                nameK = found
        target = os.path.join(rc_folder, nameS)
        if os.path.exists(target):
           os.unlink(target)
        target = os.path.join(rc_folder, nameK)
        if os.path.exists(target):
           os.unlink(target)
        return True
    def is_enabled_sysv(self, unit_file):
        name = os.path.basename(unit_file)
        target = os.path.join(self.rc3_folder(), "S50%s" % name)
        if os.path.exists(target):
           return True
        return False
    def is_enabled_of_units(self, *modules):
        result = True
        for unit in self.match_units(modules):
            if not self.is_enabled(unit):
               result = False
        return result
    def is_enabled(self, unit):
        unit_file = self.unit_file(unit)
        if self.is_sysv_file(unit_file):
            return self.is_enabled_sysv(unit_file)
        wanted = self.wanted_from(self.try_read_unit(unit))
        folder = self.enablefolder(wanted)
        if not wanted:
            return True
        target = os.path.join(folder, os.path.basename(unit_file))
        if os.path.isfile(target):
            return True
        return False
    def enabled_from(self, conf):
        unit_file = conf.filename()
        if self.is_sysv_file(unit_file):
            return self.is_enabled_sysv(unit_file)
        wanted = self.wanted_from(conf)
        folder = self.enablefolder(wanted)
        if not wanted:
            return "static"
        target = os.path.join(folder, os.path.basename(unit_file))
        if os.path.isfile(target):
            return "enabled"
        return "disabled"
    def system_daemon_reload(self):
        logg.info("ignored daemon-reload")
        return True
    def show_of_units(self, *modules):
        result = ""
        for unit in self.match_units(modules):
            if result: result += "\n\n"
            for var, value in self.show_unit_items(unit):
               if not _property or _property == var:
                   result += "%s=%s\n" % (var, value)
        if not result and modules:
            unit = modules[0]
            for var, value in self.show_unit_items(unit):
               if not _property or _property == var:
                   result += "%s=%s\n" % (var, value)
        return result
    def show_unit_items(self, unit):
        logg.info("try read unit %s", unit)
        conf = self.try_read_unit(unit)
        for entry in self.each_unit_items(unit, conf):
            yield entry
    def each_unit_items(self, unit, conf):
        yield "Id", unit
        yield "Names", unit
        yield "Description", self.get_description_from(conf) # conf.get("Unit", "Description")
        yield "MainPID", self.active_pid_from(conf) or "0"
        yield "SubState", self.active_from(conf)
        yield "ActiveState", self.is_active_from(conf) and "active" or "dead"
        yield "LoadState", conf.loaded() and "loaded" or "not-loaded"
        env_parts = []
        for env_part in conf.getlist("Service", "Environment", []):
            env_parts.append(env_part)
        if env_parts: 
            yield "Environment", " ".join(env_parts)
        env_files = []
        for env_file in conf.getlist("Service", "EnvironmentFile", []):
            env_files.append(env_file)
        if env_files:
            yield "EnvironmentFile", " ".join(env_files)
    #
    igno_centos = [ "netconsole", "network" ]
    igno_opensuse = [ "raw", "pppoe", "*.local", "boot.*", "rpmconf*" ]
    igno_ubuntu = [ "mount*", "umount*", "ondemand", "*.local" ]
    igno_always = [ "network*", "dbus", "systemd-*" ]
    def system_default_services(self, sysv="S", default_target = "multi-user.target"):
        igno = self.igno_always
        wants1_folder = os.path.join(_sysd_folder1, default_target + ".wants")
        wants2_folder = os.path.join(_sysd_folder2, default_target + ".wants")
        wants_services = []
        for unit in sorted(os.listdir(wants1_folder)):
            if unit.endswith(".service"):
                pass # wants_services.append(unit)
        for unit in sorted(os.listdir(wants2_folder)):
            if unit.endswith(".service"):
                wants_services.append(unit)
        for unit in sorted(os.listdir(self.rc3_folder())):
            m = re.match(sysv+r"\d\d(.*)", unit)
            if m:
                service = m.group(1)
                for ignore in igno:
                    if fnmatch.fnmatchcase(service, ignore):
                        continue # ignore
                wants_services.append(service)
        return wants_services
    def system_wants_services(self, sysv="S", default_target = "multi-user.target"):
        igno = self.igno_centos + self.igno_opensuse + self.igno_ubuntu + self.igno_always
        wants2_folder = os.path.join(_sysd_folder2, default_target + ".wants")
        wants_services = []
        for unit in sorted(os.listdir(wants2_folder)):
            if unit.endswith(".service"):
                wants_services.append(unit)
        for unit in sorted(os.listdir(self.rc3_folder())):
            m = re.match(sysv+r"\d\d(.*)", unit)
            if m:
                service = m.group(1)
                for ignore in igno:
                    if fnmatch.fnmatchcase(service, ignore):
                        continue # ignore
                wants_services.append(service)
        return wants_services
    def system_default(self, arg = True):
        """ start units for default system level """
        logg.info("system default requested - %s", arg)
        default_target = "multi-user.target"
        wants_services = self.system_wants_services("S", default_target)
        self.start_of_units(*wants_services)
        logg.info("system is up")
    def system_halt(self, arg = True):
        """ stop units from default system level """
        logg.info("system halt requested - %s", arg)
        default_target = "multi-user.target"
        wants_services = self.system_wants_services("K", default_target)
        self.stop_of_units(*wants_services)
        logg.info("system is down")
    def system_0(self):
        self.system_default("init 0")
        return self.system_wait("init 1")
    def system_1(self):
        self.system_default("init 1")
        return self.system_wait("init 1")
    def system_wait(self, arg = True):
        """ wait and reap children """
        signal.signal(signal.SIGTERM, lambda signum, frame: ignore_signals_and_raise_keyboard_interrupt('SIGTERM'))
        signal.signal(signal.SIGINT, lambda signum, frame: ignore_signals_and_raise_keyboard_interrupt('SIGINT'))
        while True:
            try:
                time.sleep(10)
                self.system_reap_zombies()
            except KeyboardInterrupt:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                self.system_halt(arg)
                return True
        return False
    def system_reap_zombies(self):
	for pid in os.listdir("/proc"):
	    try: pid = int(pid)
	    except: continue
	    status_file = "/proc/%s/status" % pid
	    if os.path.isfile(status_file):
	        zombie = False
	        ppid = -1
		for line in open(status_file):
		    m = re.match(r"State:\s*Z.*", line)
		    if m: zombie = True
		    m = re.match(r"PPid:\s*(\d+)", line)
		    if m: ppid = int(m.group(1))
		if zombie and ppid == os.getpid():
		    logg.info("reap zombie %s", pid)
		    try: os.waitpid(pid, os.WNOHANG)
		    except OSError, e: 
			logg.warning("reap zombie %s: %s", e.strerror)
    def system_version(self):
        return [ ("Version", __version__), ("Copyright", __copyright__) ]

if __name__ == "__main__":
    import optparse
    _o = optparse.OptionParser("%prog [options] command [name...]")
    _o.add_option("-t","--type", metavar="NAMES")
    _o.add_option("--state", metavar="STATES")
    _o.add_option("-p", "--property", metavar="PROPERTIES")
    _o.add_option("-a", "--all", action="store_true")
    _o.add_option("--reverse", action="store_true")
    _o.add_option("--after", action="store_true")
    _o.add_option("--before", action="store_true")
    _o.add_option("-l","--full", action="store_true", default=_full)
    _o.add_option("--show-types", action="store_true")
    _o.add_option("--job-mode", metavar="JOBTYPE")    
    _o.add_option("-i","--ignore-inhibitors", action="store_true")
    _o.add_option("-q","--quiet", action="store_true", default=_quiet)
    _o.add_option("--no-block", action="store_true")
    _o.add_option("--no-legend", action="store_true")
    _o.add_option("--user", action="store_true")
    _o.add_option("--system", action="store_true")
    _o.add_option("--no-wall", action="store_true")
    _o.add_option("--global", action="store_true")
    _o.add_option("--no-reload", action="store_true")
    _o.add_option("--no-ask-password", action="store_true")
    _o.add_option("--kill-who", metavar="ALL")
    _o.add_option("-s", "--signal", metavar="KILLSIG")
    _o.add_option("--force", action="store_true", default=_force)
    _o.add_option("--root", metavar="PATH")
    _o.add_option("--runtime", metavar="PROPERTY")
    _o.add_option("-n","--lines", metavar="NUMBER")
    _o.add_option("-o","--output", metavar="SHORT")
    _o.add_option("--plain", action="store_true")
    _o.add_option("-H","--host", metavar="NAME")
    _o.add_option("-M","--machine", metavar="CONTAINER")
    _o.add_option("--no-pager", action="store_true")
    _o.add_option("--version", action="store_true")
    _o.add_option("-v","--verbose", action="count", default=0)
    opt, args = _o.parse_args()
    logging.basicConfig(level = max(0, logging.FATAL - 10 * opt.verbose))
    logg.setLevel(max(0, logging.ERROR - 10 * opt.verbose))
    if os.path.exists("/var/log/systemctl.log"):
       loggfile = logging.FileHandler("/var/log/systemctl.log")
       loggfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
       logg.addHandler(loggfile)
       logg.setLevel(max(0, logging.INFO - 10 * opt.verbose))
       logg.info("EXEC BEGIN %s %s", os.path.realpath(sys.argv[0]), " ".join(args))
    if opt.version:
       args = [ "version" ]
    #
    _force = opt.force
    _quiet = opt.quiet
    _full = opt.full
    _property = getattr(opt, "property")
    #
    if not args: 
        args = [ "list-units" ]
        if os.getpid() == 0:
            args = [ "0" ]
        if os.getpid() == 1:
            args = [ "1" ]
            logg.setLevel(logging.INFO)
    command = args[0]
    modules = args[1:]
    systemctl = Systemctl()
    found = False
    # command NAME
    command_name = command.replace("-","_").replace(".","_")+"_of_unit"
    command_func = getattr(systemctl, command_name, None)
    if callable(command_func) and not found:
        found = True
        result = command_func(modules[0])
    command_name = command.replace("-","_").replace(".","_")+"_of_units"
    command_func = getattr(systemctl, command_name, None)
    if callable(command_func) and not found:
        found = True
        result = command_func(*modules)
    command_name = "show_"+command.replace("-","_").replace(".","_")
    command_func = getattr(systemctl, command_name, None)
    if callable(command_func) and not found:
        found = True
        result = command_func(*modules)
    command_name = "system_"+command.replace("-","_").replace(".","_")
    command_func = getattr(systemctl, command_name, None)
    if callable(command_func) and not found:
        found = True
        result = command_func()
        for comm in modules:
            comm_name = "system_"+comm.replace("-","_").replace(".","_")
            comm_func = getattr(systemctl, comm_name, None)
            if callable(comm_func):
                found = True
                result = comm_func()
    if not found:
        logg.error("EXEC END no method for '%s'", command)
        sys.exit(1)
    if result is None:
        logg.info("EXEC END None")
        sys.exit(0)
    elif result is True:
        logg.info("EXEC END True")
        sys.exit(0)
    elif result is False:
        logg.info("EXEC END False")
        sys.exit(1)
    elif isinstance(result, tuple) and len(result) == 2:
        exitcode, status = result
        print status
        logg.info("EXEC END %s '%s'", exitcode, status)
        if exitcode is True: exitcode = 0
        if exitcode is False: exitcode = 1
        sys.exit(exitcode)
    elif isinstance(result, basestring):
        print result
        logg.info("EXEC END '%s'", result)
    elif isinstance(result, list):
        for element in result:
            if isinstance(element, tuple):
                print "\t".join(element)
            else:
                print element
        logg.info("EXEC END %s", result)
    elif hasattr(result, "keys"):
        for key in sorted(result.keys()):
            element = result[key]
            if isinstance(element, tuple):
                print key,"=","\t".join(element)
            else:
                print key,"=",element
        logg.info("EXEC END %s", result)
    else:
        logg.warning("EXEC END Unknown result type %s", str(type(result)))
