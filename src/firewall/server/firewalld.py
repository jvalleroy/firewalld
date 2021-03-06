# -*- coding: utf-8 -*-
#
# Copyright (C) 2010-2014 Red Hat, Inc.
#
# Authors:
# Thomas Woerner <twoerner@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from gi.repository import GLib, GObject

# force use of pygobject3 in python-slip
import sys
sys.modules['gobject'] = GObject

import dbus
import dbus.service
import slip.dbus
import slip.dbus.service

from firewall.config import *
from firewall.config.dbus import *
from firewall.core.fw import Firewall
from firewall.core.rich import Rich_Rule
from firewall.core.logger import log
from firewall.server.decorators import *
from firewall.server.config import FirewallDConfig
from firewall.dbus_utils import dbus_to_python, \
    command_of_sender, context_of_sender, uid_of_sender, user_of_uid
from firewall.core.io.zone import Zone
from firewall.core.io.service import Service
from firewall.core.io.icmptype import IcmpType
from firewall.errors import *

############################################################################
#
# class FirewallD
#
############################################################################

class FirewallD(slip.dbus.service.Object):
    """FirewallD main class"""

    persistent = True
    """ Make FirewallD persistent. """
    default_polkit_auth_required = PK_ACTION_INFO
    """ Use PK_ACTION_INFO as a default """

    @handle_exceptions
    def __init__(self, *args, **kwargs):
        super(FirewallD, self).__init__(*args, **kwargs)
        self.fw = Firewall()
        self.path = args[0]
        self.start()
        self.config = FirewallDConfig(self.fw.config, self.path,
                                      DBUS_PATH_CONFIG)

    def __del__(self):
        self.stop()

    @handle_exceptions
    def start(self):
        # tests if iptables and ip6tables are usable using test functions
        # loads default firewall rules for iptables and ip6tables
        log.debug1("start()")
        self._timeouts = { }
        return self.fw.start()

    @handle_exceptions
    def stop(self):
        # stops firewall: unloads firewall modules, flushes chains and tables,
        #   resets policies
        log.debug1("stop()")
        return self.fw.stop()

    # lockdown functions

    @dbus_handle_exceptions
    def accessCheck(self, sender):
        if self.fw.policies.query_lockdown():
            if sender is None:
                log.error("Lockdown not possible, sender not set.")
                return
            bus = dbus.SystemBus()
            context = context_of_sender(bus, sender)
            if self.fw.policies.access_check("context", context):
                return
            uid = uid_of_sender(bus, sender)
            if self.fw.policies.access_check("uid", uid):
                return
            user = user_of_uid(uid)
            if self.fw.policies.access_check("user", user):
                return
            command = command_of_sender(bus, sender)
            if self.fw.policies.access_check("command", command):
                return
            raise FirewallError(ACCESS_DENIED, "lockdown is enabled")

    # timeout functions

    @dbus_handle_exceptions
    def addTimeout(self, zone, x, tag):
        if zone not in self._timeouts:
            self._timeouts[zone] = { }
        self._timeouts[zone][x] = tag

    @dbus_handle_exceptions
    def removeTimeout(self, zone, x):
        if zone in self._timeouts and x in self._timeouts[zone]:
            GLib.source_remove(self._timeouts[zone][x])
            del self._timeouts[zone][x]

    @dbus_handle_exceptions
    def cleanup_timeouts(self):
        # cleanup timeouts
        for zone in self._timeouts:
            for x in self._timeouts[zone]:
                GLib.source_remove(self._timeouts[zone][x])
            self._timeouts[zone].clear()
        self._timeouts.clear()

    # property handling

    @dbus_handle_exceptions
    def _get_property(self, prop):
        if prop == "version":
            return VERSION
        elif prop == "interface_version":
            return "%d.%d" % (DBUS_INTERFACE_VERSION,
                              DBUS_INTERFACE_REVISION)
        elif prop == "state":
            return self.fw.get_state()

        elif prop == "IPv4":
            return self.fw.ip4tables_enabled

        elif prop == "IPv6":
            return self.fw.ip6tables_enabled

        elif prop == "IPv6_rpfilter":
            return self.fw.ipv6_rpfilter_enabled

        elif prop == "BRIDGE":
            return self.fw.ebtables_enabled

        else:
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.AccessDenied: "
                "Property '%s' isn't exported (or may not exist)" % prop)

    @dbus_service_method(dbus.PROPERTIES_IFACE, in_signature='ss',
                         out_signature='v')
    @dbus_handle_exceptions
    def Get(self, interface_name, property_name, sender=None):
        # get a property
        interface_name = dbus_to_python(interface_name, str)
        property_name = dbus_to_python(property_name, str)
        log.debug1("Get('%s', '%s')", interface_name, property_name)

        if interface_name != DBUS_INTERFACE:
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.UnknownInterface: "
                "FirewallD does not implement %s" % interface_name)

        return self._get_property(property_name)

    @dbus_service_method(dbus.PROPERTIES_IFACE, in_signature='s',
                         out_signature='a{sv}')
    @dbus_handle_exceptions
    def GetAll(self, interface_name, sender=None):
        interface_name = dbus_to_python(interface_name, str)
        log.debug1("GetAll('%s')", interface_name)

        if interface_name != DBUS_INTERFACE:
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.UnknownInterface: "
                "FirewallD does not implement %s" % interface_name)

        return {
            'version': self._get_property("version"),
            'interface_version': self._get_property("interface_version"),
            'state': self._get_property("state"),
            'IPv4': self._get_property("IPv4"),
            'IPv6': self._get_property("IPv6"),
            'IPv6_rpfilter': self._get_property("IPv6_rpfilter"),
            'BRIDGE': self._get_property("BRIDGE"),
        }
        

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(dbus.PROPERTIES_IFACE, in_signature='ssv')
    @dbus_handle_exceptions
    def Set(self, interface_name, property_name, new_value, sender=None):
        interface_name = dbus_to_python(interface_name, str)
        property_name = dbus_to_python(property_name, str)
        new_value = dbus_to_python(new_value)
        log.debug1("Set('%s', '%s', '%s')", interface_name, property_name,
                   new_value)
        self.accessCheck(sender)

        if interface_name != DBUS_INTERFACE:
            raise dbus.exceptions.DBusException(
                "org.freedesktop.DBus.Error.UnknownInterface: "
                "FirewallD does not implement %s" % interface_name)

        raise dbus.exceptions.DBusException(
            "org.freedesktop.DBus.Error.AccessDenied: "
            "Property '%s' is not settable" % property_name)

    @dbus.service.signal(dbus.PROPERTIES_IFACE, signature='sa{sv}as')
    def PropertiesChanged(self, interface_name, changed_properties,
                          invalidated_properties):
        pass

    # reload

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE, in_signature='', out_signature='')
    @dbus_handle_exceptions
    def reload(self, sender=None):
        """Reload the firewall rules.
        """
        log.debug1("reload()")

        self.fw.reload()
        self.config.reload()
        self.Reloaded()

    # complete_reload

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE, in_signature='', out_signature='')
    @dbus_handle_exceptions
    def completeReload(self, sender=None):
        """Completely reload the firewall.

        Completely reload the firewall: Stops firewall, unloads modules and 
        starts the firewall again.
        """
        log.debug1("completeReload()")

        self.fw.reload(True)
        self.config.reload()
        self.Reloaded()

    @dbus.service.signal(DBUS_INTERFACE)
    @dbus_handle_exceptions
    def Reloaded(self):
        log.debug1("Reloaded()")

    # runtime to permanent

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE, in_signature='', out_signature='')
    @dbus_handle_exceptions
    def runtimeToPermanent(self, sender=None):
        """Make runtime configuration permanent
        """
        log.debug1("copyRuntimeToPermanent()")

        # Services or icmptypes can not be modified in runtime, but they can
        # be removed or modified in permanent environment. Therefore copying
        # of services and icmptypes to permanent is also needed.

        # services

        for name in self.fw.service.get_services():
            config = self.getServiceSettings(name)
            try:
                try:
                    conf_obj = self.config.getServiceByName(name)
                except FirewallError as e:
                    if "INVALID_SERVICE" in e:
                        log.debug1("Creating service '%s'" % name)
                        self.config.addService(name, config)
                    else:
                        raise
                else:
                    if conf_obj.getSettings() != config:
                        log.debug1("Copying service '%s' settings" % name)
                        conf_obj.update(config)
                    else:
                        log.debug1("Service '%s' is identical" % name)
            except Exception as e:
                raise FirewallError(RT_TO_PERM_FAILED,
                                    "service '%s' : %s" % (name, e))

        # icmptypes

        for name in self.fw.icmptype.get_icmptypes():
            config = self.getIcmpTypeSettings(name)
            try:
                try:
                    conf_obj = self.config.getIcmpTypeByName(name)
                except FirewallError as e:
                    if "INVALID_ICMPTYPE" in e:
                        log.debug1("Creating icmptype '%s'" % name)
                        self.config.addIcmpType(name, config)
                    else:
                        raise
                else:
                    if conf_obj.getSettings() != config:
                        log.debug1("Copying icmptype '%s' settings" % name)
                        conf_obj.update(config)
                    else:
                        log.debug1("IcmpType '%s' is identical" % name)
            except Exception as e:
                raise FirewallError(RT_TO_PERM_FAILED,
                                "icmptype '%s' : %s" % (name, e))

        # zones

        for name in self.fw.zone.get_zones():
            # zone runtime settings can be modified, but not service and
            # icmptye settings
            config = self.getZoneSettings(name)
            try:
                try:
                    conf_obj = self.config.getZoneByName(name)
                except FirewallError as e:
                    if "INVALID_ZONE" in e:
                        log.debug1("Creating zone '%s'" % name)
                        self.config.addZone(name, config)
                    else:
                        raise
                else:
                    if conf_obj.getSettings() != config:
                        log.debug1("Copying zone '%s' settings" % name)
                        conf_obj.update(config)
                    else:
                        log.debug1("Zone '%s' is identical" % name)
            except Exception as e:
                raise FirewallError(RT_TO_PERM_FAILED,
                                    "zone '%s' : %s" % (name, e))

        # direct

        # rt_config = self.fw.direct.get_config()
        config = ( self.fw.direct.get_all_chains(),
                   self.fw.direct.get_all_rules(),
                   self.fw.direct.get_all_passthroughs() )
        try:
            if self.config.getSettings() != config:
                log.debug1("Copying direct configuration")
                self.config.update(config)
            else:
                log.debug1("Direct configuration is identical")
        except Exception as e:
            raise FirewallError(RT_TO_PERM_FAILED,
                                "direct configuration: %s" % e)

        # policies

        config = self.fw.policies.lockdown_whitelist.export_config()
        try:
            if self.config.getSettings() != config:
                log.debug1("Copying policies configuration")
                self.config.setLockdownWhitelist(config)
            else:
                log.debug1("Policies configuration is identical")
        except Exception as e:
            raise FirewallError(RT_TO_PERM_FAILED,
                                "policies configuration: %s" % e)

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
    # POLICIES
    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # lockdown

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='',
                         out_signature='')
    @dbus_handle_exceptions
    def enableLockdown(self, sender=None):
        """Enable lockdown policies
        """
        log.debug1("policies.enableLockdown()")
        self.accessCheck(sender)
        self.fw.policies.enable_lockdown()
        self.LockdownEnabled()

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='',
                         out_signature='')
    @dbus_handle_exceptions
    def disableLockdown(self, sender=None):
        """Disable lockdown policies
        """
        log.debug1("policies.disableLockdown()")
        self.accessCheck(sender)
        self.fw.policies.disable_lockdown()
        self.LockdownDisabled()

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES_INFO)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryLockdown(self, sender=None):
        """Retuns True if lockdown is enabled
        """
        log.debug1("policies.queryLockdown()")
        # no access check here
        return self.fw.policies.query_lockdown()

    @dbus.service.signal(DBUS_INTERFACE_POLICIES, signature='')
    @dbus_handle_exceptions
    def LockdownEnabled(self):
        log.debug1("LockdownEnabled()")

    @dbus.service.signal(DBUS_INTERFACE_POLICIES, signature='')
    @dbus_handle_exceptions
    def LockdownDisabled(self):
        log.debug1("LockdownDisabled()")

    # lockdown whitelist

    # command

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='s',
                         out_signature='')
    @dbus_handle_exceptions
    def addLockdownWhitelistCommand(self, command, sender=None):
        """Add lockdown command
        """
        command = dbus_to_python(command, str)
        log.debug1("policies.addLockdownWhitelistCommand('%s')" % command)
        self.accessCheck(sender)
        self.fw.policies.lockdown_whitelist.add_command(command)
        self.LockdownWhitelistCommandAdded(command)

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='s',
                         out_signature='')
    @dbus_handle_exceptions
    def removeLockdownWhitelistCommand(self, command, sender=None):
        """Remove lockdown command
        """
        command = dbus_to_python(command, str)
        log.debug1("policies.removeLockdownWhitelistCommand('%s')" % command)
        self.accessCheck(sender)
        self.fw.policies.lockdown_whitelist.remove_command(command)
        self.LockdownWhitelistCommandRemoved(command)

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES_INFO)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='s',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryLockdownWhitelistCommand(self, command, sender=None):
        """Query lockdown command
        """
        command = dbus_to_python(command, str)
        log.debug1("policies.queryLockdownWhitelistCommand('%s')" % command)
        # no access check here
        return self.fw.policies.lockdown_whitelist.has_command(command)

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES_INFO)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='',
                         out_signature='as')
    @dbus_handle_exceptions
    def getLockdownWhitelistCommands(self, sender=None):
        """Add lockdown command
        """
        log.debug1("policies.getLockdownWhitelistCommands()")
        # no access check here
        return self.fw.policies.lockdown_whitelist.get_commands()

    @dbus.service.signal(DBUS_INTERFACE_POLICIES, signature='s')
    @dbus_handle_exceptions
    def LockdownWhitelistCommandAdded(self, command):
        log.debug1("LockdownWhitelistCommandAdded('%s')" % command)

    @dbus.service.signal(DBUS_INTERFACE_POLICIES, signature='s')
    @dbus_handle_exceptions
    def LockdownWhitelistCommandRemoved(self, command):
        log.debug1("LockdownWhitelistCommandRemoved('%s')" % command)

    # uid

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='i',
                         out_signature='')
    @dbus_handle_exceptions
    def addLockdownWhitelistUid(self, uid, sender=None):
        """Add lockdown uid
        """
        uid = dbus_to_python(uid, int)
        log.debug1("policies.addLockdownWhitelistUid('%s')" % uid)
        self.accessCheck(sender)
        self.fw.policies.lockdown_whitelist.add_uid(uid)
        self.LockdownWhitelistUidAdded(uid)

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='i',
                         out_signature='')
    @dbus_handle_exceptions
    def removeLockdownWhitelistUid(self, uid, sender=None):
        """Remove lockdown uid
        """
        uid = dbus_to_python(uid, int)
        log.debug1("policies.removeLockdownWhitelistUid('%s')" % uid)
        self.accessCheck(sender)
        self.fw.policies.lockdown_whitelist.remove_uid(uid)
        self.LockdownWhitelistUidRemoved(uid)

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES_INFO)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='i',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryLockdownWhitelistUid(self, uid, sender=None):
        """Query lockdown uid
        """
        uid = dbus_to_python(uid, int)
        log.debug1("policies.queryLockdownWhitelistUid('%s')" % uid)
        # no access check here
        return self.fw.policies.lockdown_whitelist.has_uid(uid)

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES_INFO)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='',
                         out_signature='ai')
    @dbus_handle_exceptions
    def getLockdownWhitelistUids(self, sender=None):
        """Add lockdown uid
        """
        log.debug1("policies.getLockdownWhitelistUids()")
        # no access check here
        return self.fw.policies.lockdown_whitelist.get_uids()

    @dbus.service.signal(DBUS_INTERFACE_POLICIES, signature='i')
    @dbus_handle_exceptions
    def LockdownWhitelistUidAdded(self, uid):
        log.debug1("LockdownWhitelistUidAdded(%d)" % uid)

    @dbus.service.signal(DBUS_INTERFACE_POLICIES, signature='i')
    @dbus_handle_exceptions
    def LockdownWhitelistUidRemoved(self, uid):
        log.debug1("LockdownWhitelistUidRemoved(%d)" % uid)

    # user

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='s',
                         out_signature='')
    @dbus_handle_exceptions
    def addLockdownWhitelistUser(self, user, sender=None):
        """Add lockdown user
        """
        user = dbus_to_python(user, str)
        log.debug1("policies.addLockdownWhitelistUser('%s')" % user)
        self.accessCheck(sender)
        self.fw.policies.lockdown_whitelist.add_user(user)
        self.LockdownWhitelistUserAdded(user)

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='s',
                         out_signature='')
    @dbus_handle_exceptions
    def removeLockdownWhitelistUser(self, user, sender=None):
        """Remove lockdown user
        """
        user = dbus_to_python(user, str)
        log.debug1("policies.removeLockdownWhitelistUser('%s')" % user)
        self.accessCheck(sender)
        self.fw.policies.lockdown_whitelist.remove_user(user)
        self.LockdownWhitelistUserRemoved(user)

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES_INFO)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='s',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryLockdownWhitelistUser(self, user, sender=None):
        """Query lockdown user
        """
        user = dbus_to_python(user, str)
        log.debug1("policies.queryLockdownWhitelistUser('%s')" % user)
        # no access check here
        return self.fw.policies.lockdown_whitelist.has_user(user)

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES_INFO)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='',
                         out_signature='as')
    @dbus_handle_exceptions
    def getLockdownWhitelistUsers(self, sender=None):
        """Add lockdown user
        """
        log.debug1("policies.getLockdownWhitelistUsers()")
        # no access check here
        return self.fw.policies.lockdown_whitelist.get_users()

    @dbus.service.signal(DBUS_INTERFACE_POLICIES, signature='s')
    @dbus_handle_exceptions
    def LockdownWhitelistUserAdded(self, user):
        log.debug1("LockdownWhitelistUserAdded('%s')" % user)

    @dbus.service.signal(DBUS_INTERFACE_POLICIES, signature='s')
    @dbus_handle_exceptions
    def LockdownWhitelistUserRemoved(self, user):
        log.debug1("LockdownWhitelistUserRemoved('%s')" % user)

    # context

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='s',
                         out_signature='')
    @dbus_handle_exceptions
    def addLockdownWhitelistContext(self, context, sender=None):
        """Add lockdown context
        """
        context = dbus_to_python(context, str)
        log.debug1("policies.addLockdownWhitelistContext('%s')" % context)
        self.accessCheck(sender)
        self.fw.policies.lockdown_whitelist.add_context(context)
        self.LockdownWhitelistContextAdded(context)

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='s',
                         out_signature='')
    @dbus_handle_exceptions
    def removeLockdownWhitelistContext(self, context, sender=None):
        """Remove lockdown context
        """
        context = dbus_to_python(context, str)
        log.debug1("policies.removeLockdownWhitelistContext('%s')" % context)
        self.accessCheck(sender)
        self.fw.policies.lockdown_whitelist.remove_context(context)
        self.LockdownWhitelistContextRemoved(context)

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES_INFO)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='s',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryLockdownWhitelistContext(self, context, sender=None):
        """Query lockdown context
        """
        context = dbus_to_python(context, str)
        log.debug1("policies.queryLockdownWhitelistContext('%s')" % context)
        # no access check here
        return self.fw.policies.lockdown_whitelist.has_context(context)

    @slip.dbus.polkit.require_auth(PK_ACTION_POLICIES_INFO)
    @dbus_service_method(DBUS_INTERFACE_POLICIES, in_signature='',
                         out_signature='as')
    @dbus_handle_exceptions
    def getLockdownWhitelistContexts(self, sender=None):
        """Add lockdown context
        """
        log.debug1("policies.getLockdownWhitelistContexts()")
        # no access check here
        return self.fw.policies.lockdown_whitelist.get_contexts()

    @dbus.service.signal(DBUS_INTERFACE_POLICIES, signature='s')
    @dbus_handle_exceptions
    def LockdownWhitelistContextAdded(self, context):
        log.debug1("LockdownWhitelistContextAdded('%s')" % context)

    @dbus.service.signal(DBUS_INTERFACE_POLICIES, signature='s')
    @dbus_handle_exceptions
    def LockdownWhitelistContextRemoved(self, context):
        log.debug1("LockdownWhitelistContextRemoved('%s')" % context)

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # PANIC

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE, in_signature='', out_signature='')
    @dbus_handle_exceptions
    def enablePanicMode(self, sender=None):
        """Enable panic mode.
        
        All ingoing and outgoing connections and packets will be blocked.
        """
        log.debug1("enablePanicMode()")
        self.accessCheck(sender)
        self.fw.enable_panic_mode()
        self.PanicModeEnabled()

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE, in_signature='', out_signature='')
    @dbus_handle_exceptions
    def disablePanicMode(self, sender=None):
        """Disable panic mode.

        Enables normal mode: Allowed ingoing and outgoing connections 
        will not be blocked anymore
        """
        log.debug1("disablePanicMode()")
        self.accessCheck(sender)
        self.fw.disable_panic_mode()
        self.PanicModeDisabled()

    @slip.dbus.polkit.require_auth(PK_ACTION_INFO)
    @dbus_service_method(DBUS_INTERFACE, in_signature='', out_signature='b')
    @dbus_handle_exceptions
    def queryPanicMode(self, sender=None):
        # returns True if in panic mode
        log.debug1("queryPanicMode()")
        return self.fw.query_panic_mode()

    @dbus.service.signal(DBUS_INTERFACE, signature='')
    @dbus_handle_exceptions
    def PanicModeEnabled(self):
        log.debug1("PanicModeEnabled()")

    @dbus.service.signal(DBUS_INTERFACE, signature='')
    @dbus_handle_exceptions
    def PanicModeDisabled(self):
        log.debug1("PanicModeDisabled()")

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # list functions

    @slip.dbus.polkit.require_auth(PK_ACTION_INFO)
    @dbus_service_method(DBUS_INTERFACE, in_signature='s',
                         out_signature=Zone.DBUS_SIGNATURE)
    @dbus_handle_exceptions
    def getZoneSettings(self, zone, sender=None):
        # returns zone settings for zone
        zone = dbus_to_python(zone, str)
        log.debug1("getZoneSettings(%s)", zone)
        return self.fw.zone.get_config_with_settings(zone)

    @slip.dbus.polkit.require_auth(PK_ACTION_INFO)
    @dbus_service_method(DBUS_INTERFACE, in_signature='',
                         out_signature='as')
    @dbus_handle_exceptions
    def listServices(self, sender=None):
        # returns the list of services
        # TODO: should be renamed to getServices()
        # because is called by firewall-cmd --get-services
        log.debug1("listServices()")
        return self.fw.service.get_services()

    @slip.dbus.polkit.require_auth(PK_ACTION_INFO)
    @dbus_service_method(DBUS_INTERFACE, in_signature='s',
                         out_signature=Service.DBUS_SIGNATURE)
    @dbus_handle_exceptions
    def getServiceSettings(self, service, sender=None):
        # returns service settings for service
        service = dbus_to_python(service, str)
        log.debug1("getServiceSettings(%s)", service)
        return self.fw.service.get_service(service).export_config()

    @slip.dbus.polkit.require_auth(PK_ACTION_INFO)
    @dbus_service_method(DBUS_INTERFACE, in_signature='',
                         out_signature='as')
    @dbus_handle_exceptions
    def listIcmpTypes(self, sender=None):
        # returns the list of services
        # TODO: should be renamed to getIcmptypes()
        # because is called by firewall-cmd --get-icmptypes
        log.debug1("listIcmpTypes()")
        return self.fw.icmptype.get_icmptypes()

    @slip.dbus.polkit.require_auth(PK_ACTION_INFO)
    @dbus_service_method(DBUS_INTERFACE, in_signature='s',
                         out_signature=IcmpType.DBUS_SIGNATURE)
    @dbus_handle_exceptions
    def getIcmpTypeSettings(self, icmptype, sender=None):
        # returns icmptype settings for icmptype
        icmptype = dbus_to_python(icmptype, str)
        log.debug1("getIcmpTypeSettings(%s)", icmptype)
        return self.fw.icmptype.get_icmptype(icmptype).export_config()

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # DEFAULT ZONE

    @slip.dbus.polkit.require_auth(PK_ACTION_INFO)
    @dbus_service_method(DBUS_INTERFACE, in_signature='', out_signature='s')
    @dbus_handle_exceptions
    def getDefaultZone(self, sender=None):
        # returns the system default zone
        log.debug1("getDefaultZone()")
        return self.fw.get_default_zone()

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE, in_signature='s', out_signature='')
    @dbus_handle_exceptions
    def setDefaultZone(self, zone, sender=None):
        # set the system default zone
        zone = dbus_to_python(zone, str)
        log.debug1("setDefaultZone('%s')" % zone)
        self.accessCheck(sender)
        self.fw.set_default_zone(zone)
        self.DefaultZoneChanged(zone)

    @dbus.service.signal(DBUS_INTERFACE, signature='s')
    @dbus_handle_exceptions
    def DefaultZoneChanged(self, zone):
        log.debug1("DefaultZoneChanged('%s')" % (zone))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
    # ZONE INTERFACE
    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # ZONES

    @slip.dbus.polkit.require_auth(PK_ACTION_INFO)
    # TODO: shouldn't this be in DBUS_INTERFACE instead of DBUS_INTERFACE_ZONE ?
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='',
                         out_signature='as')
    @dbus_handle_exceptions
    def getZones(self, sender=None):
        # returns the list of zones
        log.debug1("zone.getZones()")
        return self.fw.zone.get_zones()

    @slip.dbus.polkit.require_auth(PK_ACTION_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='',
                         out_signature='a{sa{sas}}')
    @dbus_handle_exceptions
    def getActiveZones(self, sender=None):
        # returns the list of active zones
        log.debug1("zone.getActiveZones()")
        zones = { }
        for zone in self.fw.zone.get_zones():
            interfaces = self.fw.zone.list_interfaces(zone)
            sources = self.fw.zone.list_sources(zone)
            if len(interfaces) + len(sources) > 0:
                zones[zone] = { }
                if len(interfaces) > 0:
                    zones[zone]["interfaces"] = interfaces
                if len(sources) > 0:
                    zones[zone]["sources"] = sources
        return zones

    @slip.dbus.polkit.require_auth(PK_ACTION_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='s')
    @dbus_handle_exceptions
    def getZoneOfInterface(self, interface, sender=None):
        """Return the zone an interface belongs to.

        :Parameters:
            `interface` : str
                Name of the interface
        :Returns: str. The name of the zone.
        """
        interface = dbus_to_python(interface, str)
        log.debug1("zone.getZoneOfInterface('%s')" % interface)
        zone = self.fw.zone.get_zone_of_interface(interface)
        if zone:
            return zone
        return ""

    @slip.dbus.polkit.require_auth(PK_ACTION_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='s')
    @dbus_handle_exceptions
    def getZoneOfSource(self, source, sender=None):
        #Return the zone an source belongs to.
        source = dbus_to_python(source, str)
        log.debug1("zone.getZoneOfSource('%s')" % source)
        zone = self.fw.zone.get_zone_of_source(source)
        if zone:
            return zone
        return ""

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='b')
    @dbus_handle_exceptions
    def isImmutable(self, zone, sender=None):
        # no immutable zones anymore
        return False

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # INTERFACES

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='s')
    @dbus_handle_exceptions
    def addInterface(self, zone, interface, sender=None):
        """Add an interface to a zone.
        If zone is empty, use default zone.
        """
        zone = dbus_to_python(zone, str)
        interface = dbus_to_python(interface, str)
        log.debug1("zone.addInterface('%s', '%s')" % (zone, interface))
        self.accessCheck(sender)
        _zone = self.fw.zone.add_interface(zone, interface, sender)

        self.InterfaceAdded(_zone, interface)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='s')
    @dbus_handle_exceptions
    def changeZone(self, zone, interface, sender=None):
        """Change a zone an interface is part of.
        If zone is empty, use default zone.

        This function is deprecated, use changeZoneOfInterface instead
        """
        zone = dbus_to_python(zone, str)
        interface = dbus_to_python(interface, str)
        return self.changeZoneOfInterface(zone, interface, sender)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='s')
    @dbus_handle_exceptions
    def changeZoneOfInterface(self, zone, interface, sender=None):
        """Change a zone an interface is part of.
        If zone is empty, use default zone.
        """
        zone = dbus_to_python(zone, str)
        interface = dbus_to_python(interface, str)
        log.debug1("zone.changeZoneOfInterface('%s', '%s')" % (zone, interface))
        self.accessCheck(sender)
        _zone = self.fw.zone.change_zone_of_interface(zone, interface, sender)

        self.ZoneOfInterfaceChanged(_zone, interface)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='s')
    @dbus_handle_exceptions
    def removeInterface(self, zone, interface, sender=None):
        """Remove interface from a zone.
        If zone is empty, remove from zone the interface belongs to.
        """
        zone = dbus_to_python(zone, str)
        interface = dbus_to_python(interface, str)
        log.debug1("zone.removeInterface('%s', '%s')" % (zone, interface))
        self.accessCheck(sender)
        _zone = self.fw.zone.remove_interface(zone, interface)

        self.InterfaceRemoved(_zone, interface)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryInterface(self, zone, interface, sender=None):
        """Return true if an interface is in a zone.
        If zone is empty, use default zone.
        """
        zone = dbus_to_python(zone, str)
        interface = dbus_to_python(interface, str)
        log.debug1("zone.queryInterface('%s', '%s')" % (zone, interface))
        return self.fw.zone.query_interface(zone, interface)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='as')
    @dbus_handle_exceptions
    def getInterfaces(self, zone, sender=None):
        """Return the list of interfaces of a zone.
        If zone is empty, use default zone.
        """
        # TODO: should be renamed to listInterfaces()
        # because is called by firewall-cmd --zone --list-interfaces
        zone = dbus_to_python(zone, str)
        log.debug1("zone.getInterfaces('%s')" % (zone))
        return self.fw.zone.list_interfaces(zone)

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ss')
    @dbus_handle_exceptions
    def InterfaceAdded(self, zone, interface):
        log.debug1("zone.InterfaceAdded('%s', '%s')" % (zone, interface))

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ss')
    @dbus_handle_exceptions
    def ZoneChanged(self, zone, interface):
        """
        This signal is deprecated.
        """
        log.debug1("zone.ZoneChanged('%s', '%s')" % (zone, interface))

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ss')
    @dbus_handle_exceptions
    def ZoneOfInterfaceChanged(self, zone, interface):
        log.debug1("zone.ZoneOfInterfaceChanged('%s', '%s')" % (zone,
                                                                interface))
        self.ZoneChanged(zone, interface)

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ss')
    @dbus_handle_exceptions
    def InterfaceRemoved(self, zone, interface):
        log.debug1("zone.InterfaceRemoved('%s', '%s')" % (zone, interface))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # SOURCES

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='s')
    @dbus_handle_exceptions
    def addSource(self, zone, source, sender=None):
        """Add a source to a zone.
        If zone is empty, use default zone.
        """
        zone = dbus_to_python(zone, str)
        source = dbus_to_python(source, str)
        log.debug1("zone.addSource('%s', '%s')" % (zone, source))
        self.accessCheck(sender)
        _zone = self.fw.zone.add_source(zone, source, sender)

        self.SourceAdded(_zone, source)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='s')
    @dbus_handle_exceptions
    def changeZoneOfSource(self, zone, source, sender=None):
        """Change a zone an source is part of.
        If zone is empty, use default zone.
        """
        zone = dbus_to_python(zone, str)
        source = dbus_to_python(source, str)
        log.debug1("zone.changeZoneOfSource('%s', '%s')" % (zone, source))
        self.accessCheck(sender)
        _zone = self.fw.zone.change_zone_of_source(zone, source, sender)

        self.ZoneOfSourceChanged(_zone, source)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='s')
    @dbus_handle_exceptions
    def removeSource(self, zone, source, sender=None):
        """Remove source from a zone.
        If zone is empty, remove from zone the source belongs to.
        """
        zone = dbus_to_python(zone, str)
        source = dbus_to_python(source, str)
        log.debug1("zone.removeSource('%s', '%s')" % (zone, source))
        self.accessCheck(sender)
        _zone = self.fw.zone.remove_source(zone, source)

        self.SourceRemoved(_zone, source)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='b')
    @dbus_handle_exceptions
    def querySource(self, zone, source, sender=None):
        """Return true if an source is in a zone.
        If zone is empty, use default zone.
        """
        zone = dbus_to_python(zone, str)
        source = dbus_to_python(source, str)
        log.debug1("zone.querySource('%s', '%s')" % (zone, source))
        return self.fw.zone.query_source(zone, source)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='as')
    @dbus_handle_exceptions
    def getSources(self, zone, sender=None):
        """Return the list of sources of a zone.
        If zone is empty, use default zone.
        """
        # TODO: should be renamed to listSources()
        # because is called by firewall-cmd --zone --list-sources
        zone = dbus_to_python(zone, str)
        log.debug1("zone.getSources('%s')" % (zone))
        return self.fw.zone.list_sources(zone)

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ss')
    @dbus_handle_exceptions
    def SourceAdded(self, zone, source):
        log.debug1("zone.SourceAdded('%s', '%s')" % (zone, source))

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ss')
    @dbus_handle_exceptions
    def ZoneOfSourceChanged(self, zone, source):
        log.debug1("zone.ZoneOfSourceChanged('%s', '%s')" % (zone, source))

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ss')
    @dbus_handle_exceptions
    def SourceRemoved(self, zone, source):
        log.debug1("zone.SourceRemoved('%s', '%s')" % (zone, source))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # RICH RULES

    @dbus_handle_exceptions
    def disableTimedRichRule(self, zone, rule):
        log.debug1("zone.disableTimedRichRule('%s', '%s')" % (zone, rule))
        del self._timeouts[zone][rule]
        obj = Rich_Rule(rule_str=rule)
        self.fw.zone.remove_rule(zone, obj)
        self.RichRuleRemoved(zone, rule)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ssi',
                         out_signature='s')
    @dbus_handle_exceptions
    def addRichRule(self, zone, rule, timeout, sender=None):
        zone = dbus_to_python(zone, str)
        rule = dbus_to_python(rule, str)
        timeout = dbus_to_python(timeout, int)
        log.debug1("zone.addRichRule('%s', '%s')" % (zone, rule))
        obj = Rich_Rule(rule_str=rule)
        _zone = self.fw.zone.add_rule(zone, obj, timeout)

        if timeout > 0:
            tag = GLib.timeout_add_seconds(timeout, self.disableTimedRichRule,
                                           _zone, rule)
            self.addTimeout(_zone, rule, tag)

        self.RichRuleAdded(_zone, rule, timeout)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='s')
    @dbus_handle_exceptions
    def removeRichRule(self, zone, rule, sender=None):
        zone = dbus_to_python(zone, str)
        rule = dbus_to_python(rule, str)
        log.debug1("zone.removeRichRule('%s', '%s')" % (zone, rule))
        obj = Rich_Rule(rule_str=rule)
        _zone = self.fw.zone.remove_rule(zone, obj)
        self.removeTimeout(_zone, rule)
        self.RichRuleRemoved(_zone, rule)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryRichRule(self, zone, rule, sender=None):
        zone = dbus_to_python(zone, str)
        rule = dbus_to_python(rule, str)
        log.debug1("zone.queryRichRule('%s', '%s')" % (zone, rule))
        obj = Rich_Rule(rule_str=rule)
        return self.fw.zone.query_rule(zone, obj)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='as')
    @dbus_handle_exceptions
    def getRichRules(self, zone, sender=None):
        # returns the list of enabled rich rules for zone
        # TODO: should be renamed to listRichRules()
        # because is called by firewall-cmd --zone --list-rich-rules
        zone = dbus_to_python(zone, str)
        log.debug1("zone.getRichRules('%s')" % (zone))
        return self.fw.zone.list_rules(zone)

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ssi')
    @dbus_handle_exceptions
    def RichRuleAdded(self, zone, rule, timeout):
        log.debug1("zone.RichRuleAdded('%s', '%s', %d)" % (zone, rule, timeout))

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ss')
    @dbus_handle_exceptions
    def RichRuleRemoved(self, zone, rule):
        log.debug1("zone.RichRuleRemoved('%s', '%s')" % (zone, rule))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # SERVICES

    @dbus_handle_exceptions
    def disableTimedService(self, zone, service):
        log.debug1("zone.disableTimedService('%s', '%s')" % (zone, service))
        del self._timeouts[zone][service]
        self.fw.zone.remove_service(zone, service)
        self.ServiceRemoved(zone, service)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ssi',
                         out_signature='s')
    @dbus_handle_exceptions
    def addService(self, zone, service, timeout, sender=None):
        # enables service <service> if not enabled already for zone
        zone = dbus_to_python(zone, str)
        service = dbus_to_python(service, str)
        timeout = dbus_to_python(timeout, int)
        log.debug1("zone.addService('%s', '%s', %d)" % (zone, service, timeout))
        self.accessCheck(sender)

        _zone = self.fw.zone.add_service(zone, service, timeout, sender)

        if timeout > 0:
            tag = GLib.timeout_add_seconds(timeout, self.disableTimedService,
                                           _zone, service)
            self.addTimeout(_zone, service, tag)

        self.ServiceAdded(_zone, service, timeout)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='s')
    @dbus_handle_exceptions
    def removeService(self, zone, service, sender=None):
        # disables service for zone
        zone = dbus_to_python(zone, str)
        service = dbus_to_python(service, str)
        log.debug1("zone.removeService('%s', '%s')" % (zone, service))
        self.accessCheck(sender)

        _zone = self.fw.zone.remove_service(zone, service)

        self.removeTimeout(_zone, service)
        self.ServiceRemoved(_zone, service)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryService(self, zone, service, sender=None):
        # returns true if a service is enabled for zone
        zone = dbus_to_python(zone, str)
        service = dbus_to_python(service, str)
        log.debug1("zone.queryService('%s', '%s')" % (zone, service))
        return self.fw.zone.query_service(zone, service)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='as')
    @dbus_handle_exceptions
    def getServices(self, zone, sender=None):
        # returns the list of enabled services for zone
        # TODO: should be renamed to listServices()
        # because is called by firewall-cmd --zone --list-services
        zone = dbus_to_python(zone, str)
        log.debug1("zone.getServices('%s')" % (zone))
        return self.fw.zone.list_services(zone)

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ssi')
    @dbus_handle_exceptions
    def ServiceAdded(self, zone, service, timeout):
        log.debug1("zone.ServiceAdded('%s', '%s', %d)" % \
                       (zone, service, timeout))

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ss')
    @dbus_handle_exceptions
    def ServiceRemoved(self, zone, service):
        log.debug1("zone.ServiceRemoved('%s', '%s')" % (zone, service))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # PORTS

    @dbus_handle_exceptions
    def disableTimedPort(self, zone, port, protocol):
        log.debug1("zone.disableTimedPort('%s', '%s', '%s')" % \
                       (zone, port, protocol))
        del self._timeouts[zone][(port, protocol)]
        self.fw.zone.remove_port(zone, port, protocol)
        self.PortRemoved(zone, port, protocol)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='sssi',
                         out_signature='s')
    @dbus_handle_exceptions
    def addPort(self, zone, port, protocol, timeout, sender=None):
        # adds port <port> <protocol> if not enabled already to zone
        zone = dbus_to_python(zone, str)
        port = dbus_to_python(port, str)
        protocol = dbus_to_python(protocol, str)
        timeout = dbus_to_python(timeout, int)
        log.debug1("zone.enablePort('%s', '%s', '%s')" % \
                       (zone, port, protocol))
        self.accessCheck(sender)
        _zone = self.fw.zone.add_port(zone, port, protocol, timeout, sender)

        if timeout > 0:
            tag = GLib.timeout_add_seconds(timeout, self.disableTimedPort,
                                           _zone, port, protocol)
            self.addTimeout(_zone, (port, protocol), tag)

        self.PortAdded(_zone, port, protocol, timeout)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='sss',
                         out_signature='s')
    @dbus_handle_exceptions
    def removePort(self, zone, port, protocol, sender=None):
        # removes port<port> <protocol> if enabled from zone
        zone = dbus_to_python(zone, str)
        port = dbus_to_python(port, str)
        protocol = dbus_to_python(protocol, str)
        log.debug1("zone.removePort('%s', '%s', '%s')" % \
                       (zone, port, protocol))
        self.accessCheck(sender)
        _zone= self.fw.zone.remove_port(zone, port, protocol)

        self.removeTimeout(_zone, (port, protocol))
        self.PortRemoved(_zone, port, protocol)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='sss',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryPort(self, zone, port, protocol, sender=None):
        # returns true if a port is enabled for zone
        zone = dbus_to_python(zone, str)
        port = dbus_to_python(port, str)
        protocol = dbus_to_python(protocol, str)
        log.debug1("zone.queryPort('%s', '%s', '%s')" % (zone, port, protocol))
        return self.fw.zone.query_port(zone, port, protocol)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='aas')
    @dbus_handle_exceptions
    def getPorts(self, zone, sender=None):
        # returns the list of enabled ports
        # TODO: should be renamed to listPorts()
        # because is called by firewall-cmd --zone --list-ports
        zone = dbus_to_python(zone, str)
        log.debug1("zone.getPorts('%s')" % (zone))
        return self.fw.zone.list_ports(zone)

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='sssi')
    @dbus_handle_exceptions
    def PortAdded(self, zone, port, protocol, timeout=0):
        log.debug1("zone.PortAdded('%s', '%s', '%s', %d)" % \
                       (zone, port, protocol, timeout))

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='sss')
    @dbus_handle_exceptions
    def PortRemoved(self, zone, port, protocol):
        log.debug1("zone.PortRemoved('%s', '%s', '%s')" % \
                       (zone, port, protocol))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # PROTOCOLS

    @dbus_handle_exceptions
    def disableTimedProtocol(self, zone, protocol):
        log.debug1("zone.disableTimedProtocol('%s', '%s')" % (zone, protocol))
        del self._timeouts[zone][(protocol)]
        self.fw.zone.remove_protocol(zone, protocol)
        self.ProtocolRemoved(zone, protocol)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ssi',
                         out_signature='s')
    @dbus_handle_exceptions
    def addProtocol(self, zone, protocol, timeout, sender=None):
        # adds protocol <protocol> if not enabled already to zone
        zone = dbus_to_python(zone, str)
        protocol = dbus_to_python(protocol, str)
        timeout = dbus_to_python(timeout, int)
        log.debug1("zone.enableProtocol('%s', '%s')" % (zone, protocol))
        self.accessCheck(sender)
        _zone = self.fw.zone.add_protocol(zone, protocol, timeout, sender)

        if timeout > 0:
            tag = GLib.timeout_add_seconds(timeout, self.disableTimedProtocol,
                                           _zone, protocol)
            self.addTimeout(_zone, protocol, tag)

        self.ProtocolAdded(_zone, protocol, timeout)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='s')
    @dbus_handle_exceptions
    def removeProtocol(self, zone, protocol, sender=None):
        # removes protocol<protocol> if enabled from zone
        zone = dbus_to_python(zone, str)
        protocol = dbus_to_python(protocol, str)
        log.debug1("zone.removeProtocol('%s', '%s')" % (zone, protocol))
        self.accessCheck(sender)
        _zone= self.fw.zone.remove_protocol(zone, protocol)

        self.removeTimeout(_zone, protocol)
        self.ProtocolRemoved(_zone, protocol)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryProtocol(self, zone, protocol, sender=None):
        # returns true if a protocol is enabled for zone
        zone = dbus_to_python(zone, str)
        protocol = dbus_to_python(protocol, str)
        log.debug1("zone.queryProtocol('%s', '%s')" % (zone, protocol))
        return self.fw.zone.query_protocol(zone, protocol)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='as')
    @dbus_handle_exceptions
    def getProtocols(self, zone, sender=None):
        # returns the list of enabled protocols
        # TODO: should be renamed to listProtocols()
        # because is called by firewall-cmd --zone --list-protocols
        zone = dbus_to_python(zone, str)
        log.debug1("zone.getProtocols('%s')" % (zone))
        return self.fw.zone.list_protocols(zone)

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ssi')
    @dbus_handle_exceptions
    def ProtocolAdded(self, zone, protocol, timeout=0):
        log.debug1("zone.ProtocolAdded('%s', '%s', %d)" % \
                       (zone, protocol, timeout))

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ss')
    @dbus_handle_exceptions
    def ProtocolRemoved(self, zone, protocol):
        log.debug1("zone.ProtocolRemoved('%s', '%s')" % (zone, protocol))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # MASQUERADE

    @dbus_handle_exceptions
    def disableTimedMasquerade(self, zone):
        del self._timeouts[zone]["masquerade"]
        self.fw.zone.remove_masquerade(zone)
        self.MasqueradeRemoved(zone)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='si',
                         out_signature='s')
    @dbus_handle_exceptions
    def addMasquerade(self, zone, timeout, sender=None):
        # adds masquerade if not added already
        zone = dbus_to_python(zone, str)
        timeout = dbus_to_python(timeout, int)
        log.debug1("zone.addMasquerade('%s')" % (zone))
        self.accessCheck(sender)
        _zone = self.fw.zone.add_masquerade(zone, timeout, sender)
        
        if timeout > 0:
            tag = GLib.timeout_add_seconds(timeout, self.disableTimedMasquerade,
                                           _zone)
            self.addTimeout(_zone, "masquerade", tag)

        self.MasqueradeAdded(_zone, timeout)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='s')
    @dbus_handle_exceptions
    def removeMasquerade(self, zone, sender=None):
        # removes masquerade
        zone = dbus_to_python(zone, str)
        log.debug1("zone.removeMasquerade('%s')" % (zone))
        self.accessCheck(sender)
        _zone = self.fw.zone.remove_masquerade(zone)

        self.removeTimeout(_zone, "masquerade")
        self.MasqueradeRemoved(_zone)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryMasquerade(self, zone, sender=None):
        # returns true if a masquerade is added
        zone = dbus_to_python(zone, str)
        log.debug1("zone.queryMasquerade('%s')" % (zone))
        return self.fw.zone.query_masquerade(zone)

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='si')
    @dbus_handle_exceptions
    def MasqueradeAdded(self, zone, timeout=0):
        log.debug1("zone.MasqueradeAdded('%s', %d)" % (zone, timeout))

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='s')
    @dbus_handle_exceptions
    def MasqueradeRemoved(self, zone):
        log.debug1("zone.MasqueradeRemoved('%s')" % (zone))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # FORWARD PORT

    @dbus_handle_exceptions
    def disable_forward_port(self, zone, port, protocol, toport, toaddr):
        del self._timeouts[zone][(port, protocol, toport, toaddr)]
        self.fw.zone.remove_forward_port(zone, port, protocol, toport, toaddr)
        self.ForwardPortRemoved(zone, port, protocol, toport, toaddr)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='sssssi',
                         out_signature='s')
    @dbus_handle_exceptions
    def addForwardPort(self, zone, port, protocol, toport, toaddr, timeout,
                       sender=None):
        # add forward port if not enabled already for zone
        zone = dbus_to_python(zone, str)
        port = dbus_to_python(port, str)
        protocol = dbus_to_python(protocol, str)
        toport = dbus_to_python(toport, str)
        toaddr = dbus_to_python(toaddr, str)
        timeout = dbus_to_python(timeout, int)
        log.debug1("zone.addForwardPort('%s', '%s', '%s', '%s', '%s')" % \
                       (zone, port, protocol, toport, toaddr))
        self.accessCheck(sender)
        _zone = self.fw.zone.add_forward_port(zone, port, protocol, toport,
                                              toaddr, timeout, sender)

        if timeout > 0:
            tag = GLib.timeout_add_seconds(timeout,
                                           self.disable_forward_port,
                                           _zone, port, protocol, toport,
                                           toaddr)
            self.addTimeout(_zone, (port, protocol, toport, toaddr), tag)

        self.ForwardPortAdded(_zone, port, protocol, toport, toaddr, timeout)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='sssss',
                         out_signature='s')
    @dbus_handle_exceptions
    def removeForwardPort(self, zone, port, protocol, toport, toaddr,
                          sender=None):
        # remove forward port from zone
        zone = dbus_to_python(zone, str)
        port = dbus_to_python(port, str)
        protocol = dbus_to_python(protocol, str)
        toport = dbus_to_python(toport, str)
        toaddr = dbus_to_python(toaddr, str)
        log.debug1("zone.removeForwardPort('%s', '%s', '%s', '%s', '%s')" % \
                       (zone, port, protocol, toport, toaddr))
        self.accessCheck(sender)
        _zone = self.fw.zone.remove_forward_port(zone, port, protocol, toport,
                                                 toaddr)

        self.removeTimeout(_zone, (port, protocol, toport, toaddr))
        self.ForwardPortRemoved(_zone, port, protocol, toport, toaddr)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='sssss',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryForwardPort(self, zone, port, protocol, toport, toaddr,
                         sender=None):
        # returns true if a forward port is enabled for zone
        zone = dbus_to_python(zone, str)
        port = dbus_to_python(port, str)
        protocol = dbus_to_python(protocol, str)
        toport = dbus_to_python(toport, str)
        toaddr = dbus_to_python(toaddr, str)
        log.debug1("zone.queryForwardPort('%s', '%s', '%s', '%s', '%s')" % \
                       (zone, port, protocol, toport, toaddr))
        return self.fw.zone.query_forward_port(zone, port, protocol, toport,
                                               toaddr)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='aas')
    @dbus_handle_exceptions
    def getForwardPorts(self, zone, sender=None):
        # returns the list of enabled ports for zone
        # TODO: should be renamed to listForwardPorts()
        # because is called by firewall-cmd --zone --list-forward-ports
        zone = dbus_to_python(zone, str)
        log.debug1("zone.getForwardPorts('%s')" % (zone))
        return self.fw.zone.list_forward_ports(zone)

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='sssssi')
    @dbus_handle_exceptions
    def ForwardPortAdded(self, zone, port, protocol, toport, toaddr,
                         timeout=0):
        log.debug1("zone.ForwardPortAdded('%s', '%s', '%s', '%s', '%s', %d)" % \
                       (zone, port, protocol, toport, toaddr, timeout))

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='sssss')
    @dbus_handle_exceptions
    def ForwardPortRemoved(self, zone, port, protocol, toport, toaddr):
        log.debug1("zone.ForwardPortRemoved('%s', '%s', '%s', '%s', '%s')" % \
                       (zone, port, protocol, toport, toaddr))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # ICMP BLOCK

    @dbus_handle_exceptions
    def disableTimedIcmpBlock(self, zone, icmp, sender):
        log.debug1("zone.disableTimedIcmpBlock('%s', '%s')" % (zone, icmp))
        del self._timeouts[zone][icmp]
        self.fw.zone.remove_icmp_block(zone, icmp)
        self.IcmpBlockRemoved(zone, icmp)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ssi',
                         out_signature='s')
    @dbus_handle_exceptions
    def addIcmpBlock(self, zone, icmp, timeout, sender=None):
        # add icmpblock <icmp> if not enabled already for zone
        zone = dbus_to_python(zone, str)
        icmp = dbus_to_python(icmp, str)
        timeout = dbus_to_python(timeout, int)
        log.debug1("zone.enableIcmpBlock('%s', '%s')" % (zone, icmp))
        self.accessCheck(sender)
        _zone = self.fw.zone.add_icmp_block(zone, icmp, timeout, sender)

        if timeout > 0:
            tag = GLib.timeout_add_seconds(timeout, self.disableTimedIcmpBlock,
                                           _zone, icmp, sender)
            self.addTimeout(_zone, icmp, tag)

        self.IcmpBlockAdded(_zone, icmp, timeout)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='s')
    @dbus_handle_exceptions
    def removeIcmpBlock(self, zone, icmp, sender=None):
        # removes icmpBlock from zone
        zone = dbus_to_python(zone, str)
        icmp = dbus_to_python(icmp, str)
        log.debug1("zone.removeIcmpBlock('%s', '%s')" % (zone, icmp))
        self.accessCheck(sender)
        _zone = self.fw.zone.remove_icmp_block(zone, icmp)

        self.removeTimeout(_zone, icmp)
        self.IcmpBlockRemoved(_zone, icmp)
        return _zone

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='ss',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryIcmpBlock(self, zone, icmp, sender=None):
        # returns true if a icmp is enabled for zone
        zone = dbus_to_python(zone, str)
        icmp = dbus_to_python(icmp, str)
        log.debug1("zone.queryIcmpBlock('%s', '%s')" % (zone, icmp))
        return self.fw.zone.query_icmp_block(zone, icmp)

    @slip.dbus.polkit.require_auth(PK_ACTION_CONFIG_INFO)
    @dbus_service_method(DBUS_INTERFACE_ZONE, in_signature='s',
                         out_signature='as')
    @dbus_handle_exceptions
    def getIcmpBlocks(self, zone, sender=None):
        # returns the list of enabled icmpblocks
        # TODO: should be renamed to listIcmpBlocks()
        # because is called by firewall-cmd --zone --list-icmp-blocks
        zone = dbus_to_python(zone, str)
        log.debug1("zone.getIcmpBlocks('%s')" % (zone))
        return self.fw.zone.list_icmp_blocks(zone)

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ssi')
    @dbus_handle_exceptions
    def IcmpBlockAdded(self, zone, icmp, timeout=0):
        log.debug1("zone.IcmpBlockAdded('%s', '%s', %d)" % \
                       (zone, icmp, timeout))

    @dbus.service.signal(DBUS_INTERFACE_ZONE, signature='ss')
    @dbus_handle_exceptions
    def IcmpBlockRemoved(self, zone, icmp):
        log.debug1("zone.IcmpBlockRemoved('%s', '%s')" % (zone, icmp))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #
    # DIRECT INTERFACE
    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # DIRECT CHAIN

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='sss',
                         out_signature='')
    @dbus_handle_exceptions
    def addChain(self, ipv, table, chain, sender=None):
        # inserts direct chain
        ipv = dbus_to_python(ipv, str)
        table = dbus_to_python(table, str)
        chain = dbus_to_python(chain, str)
        log.debug1("direct.addChain('%s', '%s', '%s')" % (ipv, table, chain))
        self.accessCheck(sender)
        self.fw.direct.add_chain(ipv, table, chain)
        self.ChainAdded(ipv, table, chain)

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='sss',
                         out_signature='')
    @dbus_handle_exceptions
    def removeChain(self, ipv, table, chain, sender=None):
        # removes direct chain
        ipv = dbus_to_python(ipv, str)
        table = dbus_to_python(table, str)
        chain = dbus_to_python(chain, str)
        log.debug1("direct.removeChain('%s', '%s', '%s')" % (ipv, table, chain))
        self.accessCheck(sender)
        self.fw.direct.remove_chain(ipv, table, chain)
        self.ChainRemoved(ipv, table, chain)
    
    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT_INFO)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='sss',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryChain(self, ipv, table, chain, sender=None):
        # returns true if a chain is enabled
        ipv = dbus_to_python(ipv, str)
        table = dbus_to_python(table, str)
        chain = dbus_to_python(chain, str)
        log.debug1("direct.queryChain('%s', '%s', '%s')" % (ipv, table, chain))
        return self.fw.direct.query_chain(ipv, table, chain)

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT_INFO)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='ss',
                         out_signature='as')
    @dbus_handle_exceptions
    def getChains(self, ipv, table, sender=None):
        # returns list of added chains
        ipv = dbus_to_python(ipv, str)
        table = dbus_to_python(table, str)
        log.debug1("direct.getChains('%s', '%s')" % (ipv, table))
        return self.fw.direct.get_chains(ipv, table)

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT_INFO)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='',
                         out_signature='a(sss)')
    @dbus_handle_exceptions
    def getAllChains(self, sender=None):
        # returns list of added chains
        log.debug1("direct.getAllChains()")
        return self.fw.direct.get_all_chains()

    @dbus.service.signal(DBUS_INTERFACE_DIRECT, signature='sss')
    @dbus_handle_exceptions
    def ChainAdded(self, ipv, table, chain):
        log.debug1("direct.ChainAdded('%s', '%s', '%s')" % (ipv, table, chain))

    @dbus.service.signal(DBUS_INTERFACE_DIRECT, signature='sss')
    @dbus_handle_exceptions
    def ChainRemoved(self, ipv, table, chain):
        log.debug1("direct.ChainRemoved('%s', '%s', '%s')" % (ipv, table, chain))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # DIRECT RULE

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='sssias',
                         out_signature='')
    @dbus_handle_exceptions
    def addRule(self, ipv, table, chain, priority, args, sender=None):
        # inserts direct rule
        ipv = dbus_to_python(ipv, str)
        table = dbus_to_python(table, str)
        chain = dbus_to_python(chain, str)
        priority = dbus_to_python(priority, int)
        args = tuple( dbus_to_python(i, str) for i in args )
        log.debug1("direct.addRule('%s', '%s', '%s', %d, '%s')" % \
                       (ipv, table, chain, priority, "','".join(args)))
        self.accessCheck(sender)
        self.fw.direct.add_rule(ipv, table, chain, priority, args)
        self.RuleAdded(ipv, table, chain, priority, args)

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='sssias',
                         out_signature='')
    @dbus_handle_exceptions
    def removeRule(self, ipv, table, chain, priority, args, sender=None):
        # removes direct rule
        ipv = dbus_to_python(ipv, str)
        table = dbus_to_python(table, str)
        chain = dbus_to_python(chain, str)
        priority = dbus_to_python(priority, int)
        args = tuple( dbus_to_python(i, str) for i in args )
        log.debug1("direct.removeRule('%s', '%s', '%s', %d, '%s')" % \
                       (ipv, table, chain, priority, "','".join(args)))
        self.accessCheck(sender)
        self.fw.direct.remove_rule(ipv, table, chain, priority, args)
        self.RuleRemoved(ipv, table, chain, priority, args)
    
    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='sss',
                         out_signature='')
    @dbus_handle_exceptions
    def removeRules(self, ipv, table, chain, sender=None):
        # removes direct rule
        ipv = dbus_to_python(ipv, str)
        table = dbus_to_python(table, str)
        chain = dbus_to_python(chain, str)
        log.debug1("direct.removeRules('%s', '%s', '%s')" % (ipv, table, chain))
        self.accessCheck(sender)
        for (priority, args) in self.fw.direct.get_rules(ipv, table, chain):
            self.fw.direct.remove_rule(ipv, table, chain, priority, args)
            self.RuleRemoved(ipv, table, chain, priority, args)

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT_INFO)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='sssias',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryRule(self, ipv, table, chain, priority, args, sender=None):
        # returns true if a rule is enabled
        ipv = dbus_to_python(ipv, str)
        table = dbus_to_python(table, str)
        chain = dbus_to_python(chain, str)
        priority = dbus_to_python(priority, int)
        args = tuple( dbus_to_python(i, str) for i in args )
        log.debug1("direct.queryRule('%s', '%s', '%s', %d, '%s')" % \
                       (ipv, table, chain, priority, "','".join(args)))
        return self.fw.direct.query_rule(ipv, table, chain, priority, args)

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT_INFO)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='sss',
                         out_signature='a(ias)')
    @dbus_handle_exceptions
    def getRules(self, ipv, table, chain, sender=None):
        # returns list of added rules
        ipv = dbus_to_python(ipv, str)
        table = dbus_to_python(table, str)
        chain = dbus_to_python(chain, str)
        log.debug1("direct.getRules('%s', '%s', '%s')" % (ipv, table, chain))
        return self.fw.direct.get_rules(ipv, table, chain)

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT_INFO)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='',
                         out_signature='a(sssias)')
    @dbus_handle_exceptions
    def getAllRules(self, sender=None):
        # returns list of added rules
        log.debug1("direct.getAllRules()")
        return self.fw.direct.get_all_rules()

    @dbus.service.signal(DBUS_INTERFACE_DIRECT, signature='sssias')
    @dbus_handle_exceptions
    def RuleAdded(self, ipv, table, chain, priority, args):
        log.debug1("direct.RuleAdded('%s', '%s', '%s', %d, '%s')" % \
                       (ipv, table, chain, priority, "','".join(args)))

    @dbus.service.signal(DBUS_INTERFACE_DIRECT, signature='sssias')
    @dbus_handle_exceptions
    def RuleRemoved(self, ipv, table, chain, priority, args):
        log.debug1("direct.RuleRemoved('%s', '%s', '%s', %d, '%s')" % \
                       (ipv, table, chain, priority, "','".join(args)))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    # DIRECT PASSTHROUGH (untracked)

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='sas',
                         out_signature='s')
    @dbus_handle_exceptions
    def passthrough(self, ipv, args, sender=None):
        # inserts direct rule
        ipv = dbus_to_python(ipv, str)
        args = tuple( dbus_to_python(i, str) for i in args )
        log.debug1("direct.passthrough('%s', '%s')" % (ipv, "','".join(args)))
        self.accessCheck(sender)
        return self.fw.direct.passthrough(ipv, args)

    # DIRECT PASSTHROUGH (tracked)

    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='sas',
                         out_signature='')
    @dbus_handle_exceptions
    def addPassthrough(self, ipv, args, sender=None):
        # inserts direct passthrough
        ipv = dbus_to_python(ipv)
        args = tuple( dbus_to_python(i) for i in args )
        log.debug1("direct.addPassthrough('%s', '%s')" % \
                   (ipv, "','".join(args)))
        self.accessCheck(sender)
        self.fw.direct.add_passthrough(ipv, args)
        self.PassthroughAdded(ipv, args)

    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='sas',
                         out_signature='')
    @dbus_handle_exceptions
    def removePassthrough(self, ipv, args, sender=None):
        # removes direct passthrough
        ipv = dbus_to_python(ipv)
        args = tuple( dbus_to_python(i) for i in args )
        log.debug1("direct.removePassthrough('%s', '%s')" % \
                       (ipv, "','".join(args)))
        self.accessCheck(sender)
        self.fw.direct.remove_passthrough(ipv, args)
        self.PassthroughRemoved(ipv, args)

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT_INFO)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='sas',
                         out_signature='b')
    @dbus_handle_exceptions
    def queryPassthrough(self, ipv, args, sender=None):
        # returns true if a passthrough is enabled
        ipv = dbus_to_python(ipv)
        args = tuple( dbus_to_python(i) for i in args )
        log.debug1("direct.queryPassthrough('%s', '%s')" % \
                       (ipv, "','".join(args)))
        return self.fw.direct.query_passthrough(ipv, args)

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT_INFO)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='',
                         out_signature='a(sas)')
    @dbus_handle_exceptions
    def getAllPassthroughs(self, sender=None):
        # returns list of all added passthroughs
        log.debug1("direct.getAllPassthroughs()")
        return self.fw.direct.get_all_passthroughs()

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='',
                         out_signature='')
    @dbus_handle_exceptions
    def removeAllPassthroughs(self, sender=None):
        # remove all passhroughs
        log.debug1("direct.removeAllPassthroughs()")
        # remove in reverse order to avoid removing non-empty chains
        for passthrough in reversed(self.getAllPassthroughs()):
            self.removePassthrough(*passthrough)

    @slip.dbus.polkit.require_auth(PK_ACTION_DIRECT_INFO)
    @dbus_service_method(DBUS_INTERFACE_DIRECT, in_signature='s',
                         out_signature='aas')
    @dbus_handle_exceptions
    def getPassthroughs(self, ipv, sender=None):
        # returns list of all added passthroughs with ipv
        ipv = dbus_to_python(ipv)
        log.debug1("direct.getPassthroughs('%s')", ipv)
        return self.fw.direct.get_passthroughs(ipv)

    @dbus.service.signal(DBUS_INTERFACE_DIRECT, signature='sas')
    @dbus_handle_exceptions
    def PassthroughAdded(self, ipv, args):
        log.debug1("direct.PassthroughAdded('%s', '%s')" % \
                       (ipv, "','".join(args)))

    @dbus.service.signal(DBUS_INTERFACE_DIRECT, signature='sas')
    @dbus_handle_exceptions
    def PassthroughRemoved(self, ipv, args):
        log.debug1("direct.PassthroughRemoved('%s', '%s')" % \
                       (ipv, "','".join(args)))

    # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # # #

    @slip.dbus.polkit.require_auth(PK_ACTION_ALL)
    @dbus_service_method(DBUS_INTERFACE, in_signature='', out_signature='')
    @dbus_handle_exceptions
    def authorizeAll(self, sender=None):
        """ PK_ACTION_ALL implies all other actions, i.e. once a subject is
            authorized for PK_ACTION_ALL it's also authorized for any other action.
            Use-case is GUI (RHBZ#994729).
        """
        pass
