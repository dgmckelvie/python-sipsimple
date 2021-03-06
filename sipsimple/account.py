# Copyright (C) 2008-2010 AG Projects. See LICENSE for details.
#

"""
Implements a SIP Account management system that allows the definition of
multiple SIP accounts and their properties.
"""

from __future__ import absolute_import, with_statement

import random
import re
import string

from itertools import chain
from time import time
from weakref import WeakKeyDictionary

from application.notification import IObserver, NotificationCenter
from application.python.util import Null, Singleton
from application.system import host
from eventlet import api, coros, proc
from eventlet.green import select
from gnutls.crypto import X509Certificate, X509PrivateKey
from gnutls.interfaces.twisted import X509Credentials
from twisted.internet import reactor
from zope.interface import implements

from sipsimple import bonjour
from sipsimple.core import ContactHeader, Credentials, Engine, FromHeader, FrozenSIPURI, Registration, RouteHeader, SIPURI, Subscription, ToHeader, PJSIPError, SIPCoreError
from sipsimple.configuration import ConfigurationManager, Setting, SettingsGroup, SettingsObject, SettingsObjectID
from sipsimple.configuration.datatypes import AudioCodecList, MSRPConnectionModel, MSRPRelayAddress, MSRPTransport, NonNegativeInteger, Path, SIPAddress, SIPProxyAddress, SRTPEncryption, STUNServerAddressList, XCAPRoot
from sipsimple.configuration.settings import SIPSimpleSettings
from sipsimple.lookup import DNSLookup, DNSLookupError
from sipsimple.payloads.messagesummary import MessageSummary, ValidationError
from sipsimple.util import Command, TimestampedNotificationData, call_in_green_thread, call_in_twisted_thread, classproperty, limit, run_in_green_thread, run_in_twisted_thread, user_info


__all__ = ['Account', 'BonjourAccount', 'AccountManager', 'AccountExists']


class ContactURI(SIPAddress):
    def __getitem__(self, transport):
        if transport in ('tls', 'tcp', 'udp'):
            parameters = {} if transport=='udp' else {'transport': transport}
            return SIPURI(user=self.username, host=self.domain, port=getattr(Engine(), '%s_port' % transport), parameters=parameters)
        return SIPAddress.__getitem__(self, transport)


class SIPRegistrationDidFail(Exception):
    def __init__(self, data):
        self.__dict__.update(data.__dict__)

class SIPRegistrationDidNotEnd(Exception):
    def __init__(self, data):
        self.__dict__.update(data.__dict__)

class SIPAccountRegistrationError(Exception):
    def __init__(self, error, timeout):
        self.error = error
        self.timeout = timeout

class SubscriptionError(Exception):
    def __init__(self, error, timeout, refresh_interval=None):
        self.error = error
        self.refresh_interval = refresh_interval
        self.timeout = timeout

class SIPSubscriptionDidFail(Exception):
    def __init__(self, data):
        self.__dict__.update(data.__dict__)

class RestartSelect(Exception): pass

class AccountExists(ValueError): pass


class AccountRegistrar(object):
    implements(IObserver)

    def __init__(self, account):
        self.account = account
        username = ''.join(random.sample(string.lowercase, 8))
        self.contact = ContactURI('%s@%s' % (username, host.default_ip))
        self.registered = False
        self._command_greenlet = None
        self._command_channel = coros.queue()
        self._data_channel = coros.queue()
        self._current_command = None
        self._dns_wait = 1
        self._refresh_timer = None
        self._register_wait = 1
        self._registration = None

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SystemIPAddressDidChange')
        notification_center.add_observer(self, name='SystemDidWakeUpFromSleep')
        self._run()

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='SystemIPAddressDidChange')
        notification_center.remove_observer(self, name='SystemDidWakeUpFromSleep')
        self._command_channel.send_exception(api.GreenletExit)

    def activate(self):
        command = Command('register')
        self._command_channel.send(command)

    def deactivate(self):
        if self._current_command is not None and self._current_command.name != 'unregister':
            api.kill(self._command_greenlet, api.GreenletExit())
            self._run()
        command = Command('unregister')
        self._command_channel.send(command)
        command.wait()

    def reactivate(self):
        self._command_channel.send(Command('unregister'))
        self._command_channel.send(Command('register'))

    def reload_settings(self):
        command = Command('reload_settings')
        self._command_channel.send(command)

    @run_in_green_thread
    def _run(self):
        self._command_greenlet = api.getcurrent()
        while True:
            self._current_command = command = self._command_channel.wait()
            handler = getattr(self, '_CH_%s' % command.name)
            handler(command)
            self._current_command = None

    def _CH_register(self, command):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()

        # Cancel any timer which would refresh the registration
        if self._refresh_timer is not None and self._refresh_timer.active():
            self._refresh_timer.cancel()
        self._refresh_timer = None

        # Initialize the registration
        if self._registration is None:
            self._registration = Registration(FromHeader(self.account.uri, self.account.display_name),
                                              credentials=self.account.credentials,
                                              duration=self.account.sip.register_interval)
            notification_center.add_observer(self, sender=self._registration)
            notification_center.post_notification('SIPAccountWillRegister', sender=self.account, data=TimestampedNotificationData())
        else:
            notification_center.post_notification('SIPAccountRegistrationWillRefresh', sender=self.account, data=TimestampedNotificationData())

        try:
            # Lookup routes
            if self.account.sip.outbound_proxy is not None:
                uri = SIPURI(host=self.account.sip.outbound_proxy.host,
                             port=self.account.sip.outbound_proxy.port,
                             parameters={'transport': self.account.sip.outbound_proxy.transport})
            else:
                uri = SIPURI(host=self.account.id.domain)
            lookup = DNSLookup()
            try:
                routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()
            except DNSLookupError, e:
                timeout = random.uniform(self._dns_wait, 2*self._dns_wait)
                self._dns_wait = limit(2*self._dns_wait, max=30)
                raise SIPAccountRegistrationError(error='DNS lookup failed: %s' % e, timeout=timeout)
            else:
                self._dns_wait = 1

            # Register by trying each route in turn
            register_timeout = time() + 30
            for route in routes:
                remaining_time = register_timeout-time()
                if remaining_time > 0:
                    # Rebuild contact according to route
                    self.contact = ContactURI('%s@%s' % (self.contact.username, host.outgoing_ip_for(route.address)))
                    contact_header = ContactHeader(self.contact[route.transport])
                    route_header = RouteHeader(route.get_uri())
                    self._registration.register(contact_header, route_header, timeout=limit(remaining_time, min=1, max=10))
                    try:
                        while True:
                            notification = self._data_channel.wait()
                            if notification.sender is self._registration and notification.name == 'SIPRegistrationDidSucceed':
                                break
                    except SIPRegistrationDidFail, e:
                        notification_center.post_notification('SIPAccountRegistrationGotAnswer', sender=self.account,
                                                              data=TimestampedNotificationData(code=e.code,
                                                                                               reason=e.reason,
                                                                                               registration=self._registration,
                                                                                               registrar=route))
                        if e.code == 401:
                            # Authentication failed, so retry the registration in some time
                            timeout = random.uniform(60, 120)
                            raise SIPAccountRegistrationError(error='Authentication failed', timeout=timeout)
                        else:
                            # Otherwise just try the next route
                            continue
                    else:
                        notification_center.post_notification('SIPAccountRegistrationGotAnswer', sender=self.account,
                                                              data=TimestampedNotificationData(code=notification.data.code,
                                                                                               reason=notification.data.reason,
                                                                                               registration=self._registration,
                                                                                               registrar=route))
                        self.registered = True
                        notification_center.post_notification('SIPAccountRegistrationDidSucceed', sender=self.account,
                                                              data=TimestampedNotificationData(contact_header=notification.data.contact_header,
                                                                                               contact_header_list=notification.data.contact_header_list,
                                                                                               expires=notification.data.expires_in,
                                                                                               registrar=route))
                        self._register_wait = 1
                        command.signal()
                        break
            else:
                # There are no more routes to try, reschedule the registration
                timeout = random.uniform(self._register_wait, 2*self._register_wait)
                self._register_wait = limit(self._register_wait*2, max=30)
                raise SIPAccountRegistrationError(error='No more routes to try', timeout=timeout)
        except SIPAccountRegistrationError, e:
            self.registered = False
            notification_center.post_notification('SIPAccountRegistrationDidFail', sender=self.account,
                                                  data=TimestampedNotificationData(error=e.error, timeout=e.timeout))
            self._refresh_timer = reactor.callLater(e.timeout, self._command_channel.send, Command('register', command.event))
            # Since we weren't able to register, recreate a registration next time
            notification_center.remove_observer(self, sender=self._registration)
            self._registration = None

    def _CH_unregister(self, command):
        notification_center = NotificationCenter()
        # Cancel any timer which would restart the registration process
        if self._refresh_timer is not None and self._refresh_timer.active():
            self._refresh_timer.cancel()
            self._refresh_timer = None
        registered = self.registered
        self.registered = False
        if self._registration is not None:
            if registered:
                self._registration.end(timeout=2)
                try:
                    while True:
                        notification = self._data_channel.wait()
                        if notification.sender is self._registration and notification.name == 'SIPRegistrationDidEnd':
                            break
                except (SIPRegistrationDidFail, SIPRegistrationDidNotEnd), e:
                    notification_center.post_notification('SIPAccountRegistrationDidNotEnd', sender=self.account,
                                                          data=TimestampedNotificationData(code=e.code,
                                                                                           reason=e.reason,
                                                                                           registration=self._registration))
                else:
                    notification_center.post_notification('SIPAccountRegistrationDidEnd', sender=self.account,
                                                          data=TimestampedNotificationData(registration=self._registration))
            notification_center.remove_observer(self, sender=self._registration)
            self._registration = None
        command.signal()

    def _CH_reload_settings(self, command):
        notification_center = NotificationCenter()
        if self._registration is not None:
            notification_center.remove_observer(self, sender=self._registration)
            self._registration = None
        self._command_channel.send(Command('register', command.event))

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPRegistrationDidSucceed(self, notification):
        self._data_channel.send(notification)

    def _NH_SIPRegistrationDidFail(self, notification):
        self._data_channel.send_exception(SIPRegistrationDidFail(notification.data))

    def _NH_SIPRegistrationWillExpire(self, notification):
        self._command_channel.send(Command('register'))

    def _NH_SIPRegistrationDidEnd(self, notification):
        self._data_channel.send(notification)

    def _NH_SIPRegistrationDidNotEnd(self, notification):
        self._data_channel.send_exception(SIPRegistrationDidNotEnd(notification.data))

    def _NH_SystemIPAddressDidChange(self, notification):
        if self._registration is not None:
            self._command_channel.send(Command('register'))

    def _NH_SystemDidWakeUpFromSleep(self, notification):
        if self._registration is not None:
            self._command_channel.send(Command('register'))


class AccountMWISubscriptionHandler(object):
    implements(IObserver)

    def __init__(self, account):
        self.account = account
        self.subscribed = False
        self.subscription = None
        self._subscription_timer = None
        self._command_greenlet = None
        self._command_channel = coros.queue()
        self._data_channel = coros.queue()
        self._current_command = None
        self.server_advertised_uri = None # this is the voicemail URI we get back from the server

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SystemIPAddressDidChange')
        notification_center.add_observer(self, name='SystemDidWakeUpFromSleep')
        self._run()

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='SystemIPAddressDidChange')
        notification_center.remove_observer(self, name='SystemDidWakeUpFromSleep')
        self._command_channel.send_exception(api.GreenletExit)

    def activate(self):
        command = Command('subscribe')
        self._command_channel.send(command)

    def deactivate(self):
        self.server_advertised_uri = None
        if self._current_command is not None and self._current_command.name != 'unsubscribe':
            api.kill(self._command_greenlet, api.GreenletExit())
            self._run()
        command = Command('unsubscribe')
        self._command_channel.send(command)
        command.wait()

    def reactivate(self):
        self._command_channel.send(Command('unsubscribe'))
        self._command_channel.send(Command('subscribe'))

    @run_in_green_thread
    def _run(self):
        self._command_greenlet = api.getcurrent()
        while True:
            self._current_command = command = self._command_channel.wait()
            handler = getattr(self, '_CH_%s' % command.name)
            handler(command)
            self._current_command = None

    def _CH_subscribe(self, command):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()

        if getattr(command, 'refresh_interval', None) is not None:
            refresh_interval = command.refresh_interval
        else:
            refresh_interval = self.account.sip.subscribe_interval

        if self._subscription_timer is not None and self._subscription_timer.active():
            self._subscription_timer.cancel()
        self._subscription_timer = None
        if self.subscription is not None:
            notification_center.remove_observer(self, sender=self.subscription)
            self.subscription.end(timeout=2)
            self.subscription = None

        try:
            # Lookup routes
            if self.account.sip.outbound_proxy is not None:
                uri = SIPURI(host=self.account.sip.outbound_proxy.host,
                             port=self.account.sip.outbound_proxy.port,
                             parameters={'transport': self.account.sip.outbound_proxy.transport})
            elif self.account.message_summary.voicemail_uri is not None and not self.account.sip.always_use_my_proxy:
                uri = SIPURI(host=self.account.message_summary.voicemail_uri.domain)
            else:
                uri = SIPURI(host=self.account.id.domain)
            lookup = DNSLookup()
            try:
                routes = lookup.lookup_sip_proxy(uri, settings.sip.transport_list).wait()
            except DNSLookupError, e:
                timeout = random.uniform(15, 30)
                raise SubscriptionError(error='DNS lookup failed: %s' % e, timeout=timeout)

            timeout = time() + 30
            for route in routes:
                remaining_time = timeout - time()
                if remaining_time > 0:
                    if self.account.message_summary.voicemail_uri is not None:
                        subscription_uri = SIPURI(user=self.account.message_summary.voicemail_uri.username, host=self.account.message_summary.voicemail_uri.domain)
                    else:
                        subscription_uri = self.account.uri
                    subscription = Subscription(FromHeader(self.account.uri, self.account.display_name),
                                                ToHeader(subscription_uri),
                                                ContactHeader(ContactURI('%s@%s' % (self.account.contact.username, host.outgoing_ip_for(route.address)))[route.transport]),
                                                'message-summary',
                                                RouteHeader(route.get_uri()),
                                                credentials=self.account.credentials,
                                                refresh=refresh_interval)
                    notification_center.add_observer(self, sender=subscription)
                    try:
                        subscription.subscribe(timeout=limit(remaining_time, min=1, max=5))
                    except (PJSIPError, SIPCoreError):
                        timeout = 5
                        raise SubscriptionError(error='Internal error', timeout=timeout)
                    try:
                        while True:
                            notification = self._data_channel.wait()
                            if notification.sender is subscription and notification.name == 'SIPSubscriptionDidStart':
                                break
                    except SIPSubscriptionDidFail, e:
                        notification_center.remove_observer(self, sender=subscription)
                        if e.code == 407:
                            # Authentication failed, so retry the subscription in some time
                            timeout = random.uniform(60, 120)
                            raise SubscriptionError(error='Authentication failed', timeout=timeout)
                        elif e.code == 423:
                            # Get the value of the Min-Expires header
                            timeout = random.uniform(60, 120)
                            if e.min_expires is not None and e.min_expires > refresh_interval:
                                raise SubscriptionError(error='Interval too short', timeout=timeout, refresh_interval=e.min_expires)
                            else:
                                timeout = 10800     # 3 hours
                                raise SubscriptionError(error='Incorrect Min-Expires header value', timeout=timeout)
                        elif e.code in (405, 406, 489):
                            timeout = 10800     # 3 hours
                            raise SubscriptionError(error='Method or event not supported', timeout=timeout)
                        else:
                            # Otherwise just try the next route
                            continue
                    else:
                        try:
                            with api.timeout(5):
                                while True:
                                    notification = self._data_channel.wait()
                                    if notification.sender is subscription and notification.name == 'SIPSubscriptionGotNotify':
                                        if notification.data.event == 'message-summary' and notification.data.body:
                                            try:
                                                message_summary = MessageSummary.parse(notification.data.body)
                                            except ValidationError:
                                                pass
                                            else:
                                                self.server_advertised_uri = message_summary.message_account and message_summary.message_account.replace('sip:', '', 1) or None
                                                notification_center.post_notification('SIPAccountMWIDidGetSummary', sender=self.account, data=TimestampedNotificationData(message_summary=message_summary))
                                        break
                        except api.TimeoutError:
                            pass
                        self.subscription = subscription
                        self.subscribed = True
                        command.signal()
                        break
            else:
                # There are no more routes to try, reschedule the subscription
                timeout = random.uniform(60, 180)
                raise SubscriptionError(error='No more routes to try', timeout=timeout)
        except SubscriptionError, e:
            self.subscribed = False
            self._subscription_timer = reactor.callLater(e.timeout, self._command_channel.send, Command('subscribe', command.event, refresh_interval=e.refresh_interval))

    def _CH_unsubscribe(self, command):
        # Cancel any timer which would restart the subscription process
        if self._subscription_timer is not None and self._subscription_timer.active():
            self._subscription_timer.cancel()
        self._subscription_timer = None
        self.subscribed = False
        if self.subscription is not None:
            subscription = self.subscription
            self.subscription = None
            subscription.end(timeout=2)
            try:
                while True:
                    notification = self._data_channel.wait()
                    if notification.sender is subscription and notification.name == 'SIPSubscriptionDidEnd':
                        break
            except SIPSubscriptionDidFail:
                pass
            NotificationCenter().remove_observer(self, sender=subscription)
        command.signal()

    @run_in_twisted_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SIPSubscriptionDidStart(self, notification):
        self._data_channel.send(notification)

    def _NH_SIPSubscriptionDidEnd(self, notification):
        self._data_channel.send(notification)

    def _NH_SIPSubscriptionDidFail(self, notification):
        if notification.sender is self.subscription:
            self._command_channel.send(Command('subscribe'))
        else:
            self._data_channel.send_exception(SIPSubscriptionDidFail(notification.data))

    def _NH_SIPSubscriptionGotNotify(self, notification):
        if self.subscription is None:
            self._data_channel.send(notification)
            return
        if notification.sender is self.subscription and notification.data.event == 'message-summary' and notification.data.body:
            try:
                message_summary = MessageSummary.parse(notification.data.body)
            except ValidationError:
                pass
            else:
                self.server_advertised_uri = message_summary.message_account and message_summary.message_account.replace('sip:', '', 1) or None
                NotificationCenter().post_notification('SIPAccountMWIDidGetSummary', sender=self.account, data=TimestampedNotificationData(message_summary=message_summary))

    def _NH_SystemIPAddressDidChange(self, notification):
        if self.subscription is not None and self._subscription_timer is None:
            self._subscription_timer = reactor.callLater(0, self._command_channel.send, Command('subscribe'))

    def _NH_SystemDidWakeUpFromSleep(self, notification):
        if self.subscription is not None and self._subscription_timer is None:
            self._subscription_timer = reactor.callLater(0, self._command_channel.send, Command('subscribe'))


class BonjourFile(object):
    instances = WeakKeyDictionary()

    def __new__(cls, file):
        if cls is BonjourFile:
            raise TypeError("BonjourFile cannot be instantiated directly")
        instance = cls.instances.get(file)
        if instance is None:
            instance = object.__new__(cls)
            instance.file = file
            instance.active = False
            cls.instances[file] = instance
        return instance

    def fileno(self):
        return self.file.fileno() if not self.closed else -1

    def close(self):
        self.file.close()
        self.file = None

    @property
    def closed(self):
        return self.file is None

    @classmethod
    def find_by_file(cls, file):
        """Return the instance matching the given DNSServiceRef file"""
        try:
            return cls.instances[file]
        except KeyError:
            raise KeyError("cannot find a %s matching the given DNSServiceRef file" % cls.__name__)


class BonjourDiscoveryFile(BonjourFile):
    def __new__(cls, file, transport):
        instance = BonjourFile.__new__(cls, file)
        instance.transport = transport
        return instance


class BonjourRegistrationFile(BonjourFile):
    def __new__(cls, file, transport):
        instance = BonjourFile.__new__(cls, file)
        instance.transport = transport
        return instance


class BonjourResolutionFile(BonjourFile):
    def __new__(cls, file, discovery_file, service_description):
        instance = BonjourFile.__new__(cls, file)
        instance.discovery_file = discovery_file
        instance.service_description = service_description
        return instance

    @property
    def transport(self):
        return self.discovery_file.transport


class BonjourServiceDescription(object):
    def __init__(self, name, type, domain):
        self.name = name
        self.type = type
        self.domain = domain

    def __repr__(self):
        return "%s(%r, %r, %r)" % (self.__class__.__name__, self.name, self.type, self.domain)

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        if isinstance(other, BonjourServiceDescription):
            return self.name==other.name and self.type==other.type and self.domain==other.domain
        else:
            return object.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(self, other)


class BonjourServices(object):
    implements(IObserver)

    def __init__(self, account):
        self.account = account
        self._stopped = True
        self._files = []
        self._neighbours = {}
        self._command_channel = coros.queue()
        self._select_proc = None
        self._discover_timer = None
        self._register_timer = None
        self._update_timer = None

    def start(self):
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='SystemIPAddressDidChange')
        notification_center.add_observer(self, name='SystemDidWakeUpFromSleep')
        self._select_proc = proc.spawn(self._process_files)
        proc.spawn(self._handle_commands)

    def stop(self):
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='SystemIPAddressDidChange')
        notification_center.remove_observer(self, name='SystemDidWakeUpFromSleep')
        self._select_proc.kill()
        self._command_channel.send_exception(api.GreenletExit)

    def activate(self):
        self._stopped = False
        self._command_channel.send(Command('register'))
        self._command_channel.send(Command('discover'))

    def deactivate(self):
        command = Command('stop')
        self._command_channel.send(command)
        command.wait()
        self._stopped = True

    def restart_discovery(self):
        self._command_channel.send(Command('discover'))

    def restart_registration(self):
        self._command_channel.send(Command('unregister'))
        self._command_channel.send(Command('register'))

    def update_registrations(self):
        self._command_channel.send(Command('update_registrations'))

    def _register_cb(self, file, flags, error_code, name, regtype, domain):
        notification_center = NotificationCenter()
        file = BonjourRegistrationFile.find_by_file(file)
        if error_code == bonjour.kDNSServiceErr_NoError:
            notification_center.post_notification('BonjourAccountRegistrationDidSucceed', sender=self.account,
                                                  data=TimestampedNotificationData(name=name, transport=file.transport))
        else:
            error = bonjour.BonjourError(error_code)
            notification_center.post_notification('BonjourAccountRegistrationDidFail', sender=self.account,
                                                  data=TimestampedNotificationData(reason=str(error), transport=file.transport))
            self._files.remove(file)
            self._select_proc.kill(RestartSelect)
            file.close()
            if self._register_timer is None:
                self._register_timer = reactor.callLater(1, self._command_channel.send, Command('register'))

    def _browse_cb(self, file, flags, interface_index, error_code, service_name, regtype, reply_domain):
        notification_center = NotificationCenter()
        file = BonjourDiscoveryFile.find_by_file(file)
        service_description = BonjourServiceDescription(service_name, regtype, reply_domain)
        if error_code != bonjour.kDNSServiceErr_NoError:
            error = bonjour.BonjourError(error_code)
            notification_center.post_notification('BonjourAccountDiscoveryDidFail', sender=self.account, data=TimestampedNotificationData(reason=str(error), transport=file.transport))
            removed_files = [file] + [f for f in self._files if isinstance(f, BonjourResolutionFile) and f.discovery_file==file]
            for f in removed_files:
                self._files.remove(f)
            self._select_proc.kill(RestartSelect)
            for f in removed_files:
                f.close()
            if self._discover_timer is None:
                self._discover_timer = reactor.callLater(1, self._command_channel.send, Command('discover'))
            return
        if flags & bonjour.kDNSServiceFlagsAdd:
            try:
                resolution_file = (f for f in self._files if isinstance(f, BonjourResolutionFile) and f.discovery_file==file and f.service_description==service_description).next()
            except StopIteration:
                try:
                    resolution_file = bonjour.DNSServiceResolve(0, interface_index, service_name, regtype, reply_domain, self._resolve_cb)
                except bonjour.BonjourError, e:
                    notification_center.post_notification('BonjourAccountDiscoveryFailure', sender=self.account, data=TimestampedNotificationData(error=str(e), transport=file.transport))
                else:
                    resolution_file = BonjourResolutionFile(resolution_file, discovery_file=file, service_description=service_description)
                    self._files.append(resolution_file)
                    self._select_proc.kill(RestartSelect)
        else:
            try:
                resolution_file = (f for f in self._files if isinstance(f, BonjourResolutionFile) and f.discovery_file==file and f.service_description==service_description).next()
            except StopIteration:
                pass
            else:
                self._files.remove(resolution_file)
                self._select_proc.kill(RestartSelect)
                resolution_file.close()
                service_description = resolution_file.service_description
                if service_description in self._neighbours:
                    del self._neighbours[service_description]
                    notification_center.post_notification('BonjourAccountDidRemoveNeighbour', sender=self.account, data=TimestampedNotificationData(neighbour=service_description))

    def _resolve_cb(self, file, flags, interface_index, error_code, fullname, host_target, port, txtrecord):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()
        file = BonjourResolutionFile.find_by_file(file)
        if error_code == bonjour.kDNSServiceErr_NoError:
            txt = bonjour.TXTRecord.parse(txtrecord)
            display_name = txt['name'].decode('utf-8') if 'name' in txt else None
            host = re.match(r'^(.*?)(\.local)?\.?$', host_target).group(1)
            contact = txt.get('contact', file.service_description.name).split(None, 1)[0].strip('<>')
            try:
                uri = FrozenSIPURI.parse(contact)
            except SIPCoreError:
                pass
            else:
                service_description = file.service_description
                transport = uri.parameters.get('transport', 'udp')
                supported_transport = transport in settings.sip.transport_list and (transport!='tls' or self.account.tls.certificate is not None)
                if supported_transport and uri != self.account.contact[transport]:
                    notification_name = 'BonjourAccountDidUpdateNeighbour' if service_description in self._neighbours else 'BonjourAccountDidAddNeighbour'
                    notification_data = TimestampedNotificationData(neighbour=service_description, display_name=display_name, host=host, uri=uri)
                    self._neighbours[service_description] = uri
                    notification_center.post_notification(notification_name, sender=self.account, data=notification_data)
                elif service_description in self._neighbours:
                    del self._neighbours[service_description]
                    notification_center.post_notification('BonjourAccountDidRemoveNeighbour', sender=self.account, data=TimestampedNotificationData(neighbour=service_description))
        else:
            self._files.remove(file)
            self._select_proc.kill(RestartSelect)
            file.close()
            error = bonjour.BonjourError(error_code)
            notification_center.post_notification('BonjourAccountDiscoveryFailure', sender=self.account, data=TimestampedNotificationData(error=str(error), transport=file.transport))
            # start a new resolve process here? -Dan

    def _process_files(self):
        while True:
            try:
                ready = select.select([f for f in self._files if not f.active and not f.closed], [], [])[0]
            except RestartSelect:
                continue
            else:
                for file in ready:
                    file.active = True
                self._command_channel.send(Command('process_results', files=[f for f in ready if not f.closed]))

    def _handle_commands(self):
        while True:
            command = self._command_channel.wait()
            if not self._stopped:
                handler = getattr(self, '_CH_%s' % command.name)
                handler(command)

    def _CH_unregister(self, command):
        if self._register_timer is not None and self._register_timer.active():
            self._register_timer.cancel()
        self._register_timer = None
        if self._update_timer is not None and self._update_timer.active():
            self._update_timer.cancel()
        self._update_timer = None
        old_files = []
        for file in (f for f in self._files[:] if isinstance(f, BonjourRegistrationFile)):
            old_files.append(file)
            self._files.remove(file)
        self._select_proc.kill(RestartSelect)
        for file in old_files:
            file.close()
        notification_center = NotificationCenter()
        for transport in set(file.transport for file in self._files):
            notification_center.post_notification('BonjourAccountRegistrationDidEnd', sender=self.account, data=TimestampedNotificationData(transport=transport))
        command.signal()

    def _CH_register(self, command):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()
        if self._register_timer is not None and self._register_timer.active():
            self._register_timer.cancel()
        self._register_timer = None
        supported_transports = set(transport for transport in settings.sip.transport_list if transport!='tls' or self.account.tls.certificate is not None)
        registered_transports = set(file.transport for file in self._files if isinstance(file, BonjourRegistrationFile))
        missing_transports = supported_transports - registered_transports
        added_transports = set()
        for transport in missing_transports:
            notification_center.post_notification('BonjourAccountWillRegister', sender=self.account, data=TimestampedNotificationData(transport=transport))
            contact = self.account.contact[transport]
            # When account.display_name will be a unicode object, an encode('utf-8') is required here. -Luci
            txtdata = dict(txtvers=1, name=self.account.display_name, contact="<%s>" % str(contact))
            try:
                file = bonjour.DNSServiceRegister(name=str(contact),
                                                  regtype="_sipuri._%s" % (transport if transport == 'udp' else 'tcp'),
                                                  port=contact.port,
                                                  callBack=self._register_cb,
                                                  txtRecord=bonjour.TXTRecord(items=txtdata))
            except bonjour.BonjourError, e:
                notification_center.post_notification('BonjourAccountRegistrationDidFail', sender=self.account,
                                                      data=TimestampedNotificationData(reason=str(e), transport=transport))
            else:
                self._files.append(BonjourRegistrationFile(file, transport))
                added_transports.add(transport)
        if added_transports:
            self._select_proc.kill(RestartSelect)
        if added_transports != missing_transports:
            self._register_timer = reactor.callLater(1, self._command_channel.send, Command('register', command.event))
        else:
            command.signal()

    def _CH_update_registrations(self, command):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()
        if self._update_timer is not None and self._update_timer.active():
            self._update_timer.cancel()
        self._update_timer = None
        available_transports = settings.sip.transport_list
        old_files = []
        for file in (f for f in self._files[:] if isinstance(f, BonjourRegistrationFile) and f.transport not in available_transports):
            old_files.append(file)
            self._files.remove(file)
        self._select_proc.kill(RestartSelect)
        for file in old_files:
            file.close()
        update_failure = False
        for file in (f for f in self._files if isinstance(f, BonjourRegistrationFile)):
            contact = self.account.contact[file.transport]
            # When account.display_name will be a unicode object, an encode('utf-8') is required here. -Luci
            txtdata = dict(txtvers=1, name=self.account.display_name, contact="<%s>" % str(contact))
            try:
                bonjour.DNSServiceUpdateRecord(file.file, None, flags=0, rdata=bonjour.TXTRecord(items=txtdata), ttl=0)
            except bonjour.BonjourError, e:
                notification_center.post_notification('BonjourAccountRegistrationUpdateDidFail', sender=self.account,
                                                      data=TimestampedNotificationData(reason=str(e), transport=file.transport))
                update_failure = True
        self._command_channel.send(Command('register'))
        if update_failure:
            self._update_timer = reactor.callLater(1, self._command_channel.send, Command('update_registrations', command.event))
        else:
            command.signal()

    def _CH_discover(self, command):
        notification_center = NotificationCenter()
        settings = SIPSimpleSettings()
        if self._discover_timer is not None and self._discover_timer.active():
            self._discover_timer.cancel()
        self._discover_timer = None
        supported_transports = set(transport for transport in settings.sip.transport_list if transport!='tls' or self.account.tls.certificate is not None)
        discoverable_transports = set('tcp' if transport=='tls' else transport for transport in supported_transports)
        old_files = []
        for file in (f for f in self._files[:] if isinstance(f, (BonjourDiscoveryFile, BonjourResolutionFile)) and f.transport not in discoverable_transports):
            old_files.append(file)
            self._files.remove(file)
        self._select_proc.kill(RestartSelect)
        for file in old_files:
            file.close()
        for service_description in [service for service, uri in self._neighbours.iteritems() if uri.parameters.get('transport', 'udp') not in supported_transports]:
            del self._neighbours[service_description]
            notification_center.post_notification('BonjourAccountDidRemoveNeighbour', sender=self.account, data=TimestampedNotificationData(neighbour=service_description))
        discovered_transports = set(file.transport for file in self._files if isinstance(file, BonjourDiscoveryFile))
        missing_transports = discoverable_transports - discovered_transports
        added_transports = set()
        for transport in missing_transports:
            notification_center.post_notification('BonjourAccountWillInitiateDiscovery', sender=self.account, data=TimestampedNotificationData(transport=transport))
            try:
                file = bonjour.DNSServiceBrowse(regtype="_sipuri._%s" % transport, callBack=self._browse_cb)
            except bonjour.BonjourError, e:
                notification_center.post_notification('BonjourAccountDiscoveryDidFail', sender=self.account, data=TimestampedNotificationData(reason=str(e), transport=transport))
            else:
                self._files.append(BonjourDiscoveryFile(file, transport))
                added_transports.add(transport)
        if added_transports:
            self._select_proc.kill(RestartSelect)
        if added_transports != missing_transports:
            self._discover_timer = reactor.callLater(1, self._command_channel.send, Command('discover', command.event))
        else:
            command.signal()

    def _CH_process_results(self, command):
        for file in (f for f in command.files if not f.closed):
            try:
                bonjour.DNSServiceProcessResult(file.file)
            except:
                # Should we close the file? The documentation doesn't say anything about this. -Luci
                import traceback
                traceback.print_exc()
        for file in command.files:
            file.active = False
        self._files = [f for f in self._files if not f.closed]
        self._select_proc.kill(RestartSelect)

    def _CH_stop(self, command):
        if self._discover_timer is not None and self._discover_timer.active():
            self._discover_timer.cancel()
        self._discover_timer = None
        if self._register_timer is not None and self._register_timer.active():
            self._register_timer.cancel()
        self._register_timer = None
        if self._update_timer is not None and self._update_timer.active():
            self._update_timer.cancel()
        self._update_timer = None
        old_files = self._files
        self._files = []
        self._select_proc.kill(RestartSelect)
        self._neighbours = {}
        for file in old_files:
            file.close()
        notification_center = NotificationCenter()
        for transport in set(file.transport for file in self._files):
            notification_center.post_notification('BonjourAccountRegistrationDidEnd', sender=self.account, data=TimestampedNotificationData(transport=transport))
        command.signal()

    @run_in_green_thread
    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_SystemIPAddressDidChange(self, notification):
        if self._files:
            self.restart_discovery()
            self.restart_registration()

    def _NH_SystemDidWakeUpFromSleep(self, notification):
        if self._files:
            self.restart_discovery()
            self.restart_registration()


class AuthSettings(SettingsGroup):
    username = Setting(type=str, default=None, nillable=True)
    password = Setting(type=str, default='')


class SIPSettings(SettingsGroup):
    always_use_my_proxy = Setting(type=bool, default=False)
    outbound_proxy = Setting(type=SIPProxyAddress, default=None, nillable=True)
    register = Setting(type=bool, default=True)
    register_interval = Setting(type=NonNegativeInteger, default=600)
    subscribe_interval = Setting(type=NonNegativeInteger, default=3600)
    publish_interval = Setting(type=NonNegativeInteger, default=3600)


class RTPSettings(SettingsGroup):
    audio_codec_list = Setting(type=AudioCodecList, default=None, nillable=True)
    srtp_encryption = Setting(type=SRTPEncryption, default='disabled')
    use_srtp_without_tls = Setting(type=bool, default=False)


class DialogEventSettings(SettingsGroup):
    enabled = Setting(type=bool, default=False)


class NATTraversalSettings(SettingsGroup):
    use_ice = Setting(type=bool, default=False)
    stun_server_list = Setting(type=STUNServerAddressList, default=None, nillable=True)
    msrp_relay = Setting(type=MSRPRelayAddress, default=None, nillable=True)
    use_msrp_relay_for_inbound = Setting(type=bool, default=True)
    use_msrp_relay_for_outbound = Setting(type=bool, default=False)


class MessageSummarySettings(SettingsGroup):
    enabled = Setting(type=bool, default=False)
    voicemail_uri = Setting(type=SIPAddress, default=None, nillable=True)


class XCAPSettings(SettingsGroup):
    enabled = Setting(type=bool, default=False)
    xcap_root = Setting(type=XCAPRoot, default=None, nillable=True)


class PresenceSettings(SettingsGroup):
    enabled = Setting(type=bool, default=False)
    use_rls = Setting(type=bool, default=False)


class TLSSettings(SettingsGroup):
    certificate = Setting(type=Path, default=None, nillable=True)
    verify_server = Setting(type=bool, default=False)


class MSRPSettings(SettingsGroup):
    transport = Setting(type=MSRPTransport, default='tls')
    connection_model = Setting(type=MSRPConnectionModel, default='relay')


class AccountID(SettingsObjectID):
    def __init__(self):
        SettingsObjectID.__init__(self, type=SIPAddress)

    def __set__(self, account, id):
        if not isinstance(id, self.type):
            id = self.type(id)
        # check whether the old value is the same as the new value
        if account in self.values and self.values[account] == id:
            return
        if AccountManager().has_account(id):
            raise AccountExists('SIP address in use by another account')
        SettingsObjectID.__set__(self, account, id)


class Account(SettingsObject):
    """
    Object represeting a SIP account. Contains configuration settings and
    attributes for accessing SIP related objects.

    When the account is active, it will register, publish its presence and
    subscribe to watcher-info events depending on its settings.

    If the object is unpickled and its enabled flag was set, it will
    automatically activate.

    When the save method is called, depending on the value of the enabled flag,
    the account will activate/deactivate.

    Notifications sent by instances of Account:
     * CFGSettingsObjectDidChange
     * SIPAccountWillActivate
     * SIPAccountDidActivate
     * SIPAccountWillDeactivate
     * SIPAccountDidDeactivate
    """

    implements(IObserver)

    __group__ = 'Accounts'
    __id__ = AccountID()

    id = __id__
    enabled = Setting(type=bool, default=False)
    display_name = Setting(type=str, default=None, nillable=True)

    auth = AuthSettings
    sip = SIPSettings
    rtp = RTPSettings
    dialog_event = DialogEventSettings
    nat_traversal = NATTraversalSettings
    message_summary = MessageSummarySettings
    msrp = MSRPSettings
    presence = PresenceSettings
    xcap = XCAPSettings
    tls = TLSSettings

    def __init__(self, id):
        self._active = False
        self._registrar = AccountRegistrar(self)
        self._mwi_handler = AccountMWISubscriptionHandler(self)
        self._started = False

        manager = AccountManager()
        manager._internal_add_account(self)

        from sipsimple.application import SIPApplication
        if SIPApplication.running:
            call_in_twisted_thread(self.start)

    def start(self):
        if self._started:
            return
        self._started = True

        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange', sender=self)
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange', sender=SIPSimpleSettings())

        self._registrar.start()
        self._mwi_handler.start()
        if self.enabled:
            self._activate()

    def stop(self):
        if not self._started:
            return
        self._started = False

        self._deactivate()
        self._mwi_handler.stop()
        self._mwi_handler = None
        self._registrar.stop()
        self._registrar = None

        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='CFGSettingsObjectDidChange', sender=self)
        notification_center.remove_observer(self, name='CFGSettingsObjectDidChange', sender=SIPSimpleSettings())

    def delete(self):
        call_in_green_thread(self.stop)
        SettingsObject.delete(self)

        manager = AccountManager()
        manager._internal_remove_account(self)

    @run_in_green_thread
    def reregister(self):
        if self._started and self.sip.register:
            self._registrar.reactivate()

    @property
    def contact(self):
        return self._registrar.contact

    @property
    def credentials(self):
        return Credentials(self.auth.username or self.id.username, self.auth.password)

    @property
    def registered(self):
        return self._registrar.registered if self._registrar else False

    @property
    def mwi_active(self):
        return self._mwi_handler.subscribed if self._mwi_handler else False

    @property
    def tls_credentials(self):
        # This property can be optimized to cache the credentials it loads from disk,
        # however this is not a time consuming operation (~ 3000 req/sec). -Luci
        settings = SIPSimpleSettings()
        if self.tls.certificate is not None:
            certificate_data = open(self.tls.certificate.normalized).read()
            certificate = X509Certificate(certificate_data)
            private_key = X509PrivateKey(certificate_data)
        else:
            certificate = None
            private_key = None
        if settings.tls.ca_list is not None:
            # we should read all certificates in the file, rather than just the first -Luci
            trusted = [X509Certificate(open(settings.tls.ca_list.normalized).read())]
        else:
            trusted = []
        credentials = X509Credentials(certificate, private_key, trusted)
        credentials.verify_peer = self.tls.verify_server
        return credentials

    @property
    def uri(self):
        return SIPURI(user=self.id.username, host=self.id.domain)

    @property
    def voicemail_uri(self):
        return self._mwi_handler and self._mwi_handler.server_advertised_uri or self.message_summary.voicemail_uri

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @run_in_green_thread
    def _NH_CFGSettingsObjectDidChange(self, notification):
        # activate/deactivate the account or start/stop/reload the registration process
        if self._started:
            if 'enabled' in notification.data.modified:
                if self.enabled:
                    self._activate()
                else:
                    self._deactivate()
            elif self.enabled:
                registrar_attributes = ['auth.password', 'auth.username', 'sip.outbound_proxy', 'sip.transport_list', 'sip.register_interval']
                voicemail_attributes = ['auth.password', 'auth.username', 'sip.always_use_my_proxy', 'sip.outbound_proxy', 'sip.transport_list', 'sip.subscribe_interval', 'message_summary.voicemail_uri']
                if 'sip.register' in notification.data.modified:
                    if self.sip.register:
                        self._registrar.activate()
                    else:
                        self._registrar.deactivate()
                elif self.sip.register and set(registrar_attributes).intersection(notification.data.modified):
                    self._registrar.reload_settings()
                if 'message_summary.enabled' in notification.data.modified:
                    if self.message_summary.enabled:
                        self._mwi_handler.activate()
                    else:
                        self._mwi_handler.deactivate()
                elif self.message_summary.enabled and set(voicemail_attributes).intersection(notification.data.modified):
                    self._mwi_handler.activate()

    @run_in_green_thread
    def _NH_CFGSettingsObjectDidChangeID(self, notification):
        if self._started and self.enabled:
            if self.sip.register:
                self._registrar.reload_settings()
            if self.message_summary.enabled:
                self._mwi_handler.activate()

    def _activate(self):
        if self._active:
            return
        notification_center = NotificationCenter()
        notification_center.post_notification('SIPAccountWillActivate', sender=self, data=TimestampedNotificationData())
        self._active = True
        if self.sip.register:
            self._registrar.activate()
        if self.message_summary.enabled:
            self._mwi_handler.activate()
        notification_center.post_notification('SIPAccountDidActivate', sender=self, data=TimestampedNotificationData())

    def _deactivate(self):
        if not self._active:
            return
        notification_center = NotificationCenter()
        notification_center.post_notification('SIPAccountWillDeactivate', sender=self, data=TimestampedNotificationData())
        self._active = False
        self._mwi_handler.deactivate()
        self._registrar.deactivate()
        notification_center.post_notification('SIPAccountDidDeactivate', sender=self, data=TimestampedNotificationData())

    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.id)
    __str__ = __repr__

    def __setstate__(self, data):
        # This restores the password from its previous location as a top level setting
        # after it was moved under the auth group.
        SettingsObject.__setstate__(self, data)
        if not data.get('auth', {}).get('password') and data.get('password'):
            self.auth.password = data.pop('password')
            self.save()


class BonjourMSRPSettings(SettingsGroup):
    transport = Setting(type=MSRPTransport, default='tcp')


class BonjourAccountEnabledSetting(Setting):
    def __get__(self, obj, objtype):
        if obj is None:
            return self
        return bonjour.available and self.values.get(obj, self.default)

    def __set__(self, obj, value):
        if not bonjour.available:
            raise RuntimeError('mdns support is not available')
        Setting.__set__(self, obj, value)


class BonjourAccount(SettingsObject):
    """
    Object represeting a bonjour account. Contains configuration settings and
    attributes for accessing bonjour related options.

    When the account is active, it will send broadcast its contact address on
    the LAN.

    If the object is unpickled and its enabled flag was set, it will
    automatically activate.

    When the save method is called, depending on the value of the enabled flag,
    the account will activate/deactivate.

    Notifications sent by instances of Account:
     * CFGSettingsObjectDidChange
     * SIPAccountWillActivate
     * SIPAccountDidActivate
     * SIPAccountWillDeactivate
     * SIPAccountDidDeactivate
    """

    __metaclass__ = Singleton

    implements(IObserver)

    __group__ = 'Accounts'
    __id__ = SIPAddress('bonjour@local')

    id = property(lambda self: self.__id__)
    enabled = BonjourAccountEnabledSetting(type=bool, default=True)
    display_name = Setting(type=str, default=user_info.fullname, nillable=False)

    rtp = RTPSettings
    msrp = BonjourMSRPSettings
    tls = TLSSettings

    def __init__(self):
        username = ''.join(random.sample(string.lowercase, 8))
        self.contact = ContactURI('%s@%s' % (username, host.default_ip))
        self.credentials = None

        self._active = False
        self._started = False
        self._bonjour_services = BonjourServices(self)

        # initialize nat settings
        self.nat_traversal = NATTraversalSettings()
        self.nat_traversal.use_ice = False
        self.nat_traversal.msrp_relay = None
        self.nat_traversal.use_msrp_relay_for_inbound = False
        self.nat_traversal.use_msrp_relay_for_outbound = False

        from sipsimple.application import SIPApplication
        if SIPApplication.running:
            call_in_twisted_thread(self.start)

    def start(self):
        if self._started:
            return
        self._started = True

        settings = SIPSimpleSettings()
        notification_center = NotificationCenter()
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange', sender=self)
        notification_center.add_observer(self, name='CFGSettingsObjectDidChange', sender=settings)

        self._bonjour_services.start()
        if self.enabled:
            self._activate()

    def stop(self):
        if not self._started:
            return
        self._started = False

        self._deactivate()
        self._bonjour_services.stop()

        settings = SIPSimpleSettings()
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, name='CFGSettingsObjectDidChange', sender=self)
        notification_center.remove_observer(self, name='CFGSettingsObjectDidChange', sender=settings)

    @classproperty
    def mdns_available(cls):
        return bonjour.available

    @property
    def registered(self):
        return False

    @property
    def tls_credentials(self):
        # This property can be optimized to cache the credentials it loads from disk,
        # however this is not a time consuming operation (~ 3000 req/sec). -Luci
        settings = SIPSimpleSettings()
        if self.tls.certificate is not None:
            certificate_data = open(self.tls.certificate.normalized).read()
            certificate = X509Certificate(certificate_data)
            private_key = X509PrivateKey(certificate_data)
        else:
            certificate = None
            private_key = None
        if settings.tls.ca_list is not None:
            # we should read all certificates in the file, rather than just the first -Luci
            trusted = [X509Certificate(open(settings.tls.ca_list.normalized).read())]
        else:
            trusted = []
        credentials = X509Credentials(certificate, private_key, trusted)
        credentials.verify_peer = self.tls.verify_server
        return credentials

    @property
    def uri(self):
        return SIPURI(user=self.contact.username, host=self.contact.domain)

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    @run_in_green_thread
    def _NH_CFGSettingsObjectDidChange(self, notification):
        if self._started:
            if 'enabled' in notification.data.modified:
                if self.enabled:
                    self._activate()
                else:
                    self._deactivate()
            elif self.enabled:
                if 'display_name' in notification.data.modified:
                    self._bonjour_services.update_registrations()
                if set(['sip.transport_list', 'tls.certificate']).intersection(notification.data.modified):
                    self._bonjour_services.update_registrations()
                    self._bonjour_services.restart_discovery()

    def _activate(self):
        if self._active:
            return
        notification_center = NotificationCenter()
        notification_center.post_notification('SIPAccountWillActivate', sender=self, data=TimestampedNotificationData())
        self._active = True
        self._bonjour_services.activate()
        notification_center.post_notification('SIPAccountDidActivate', sender=self, data=TimestampedNotificationData())

    def _deactivate(self):
        if not self._active:
            return
        notification_center = NotificationCenter()
        notification_center.post_notification('SIPAccountWillDeactivate', sender=self, data=TimestampedNotificationData())
        self._active = False
        self._bonjour_services.deactivate()
        notification_center.post_notification('SIPAccountDidDeactivate', sender=self, data=TimestampedNotificationData())

    def __repr__(self):
        return '%s()' % self.__class__.__name__
    __str__ = __repr__


class AccountManager(object):
    """
    This is a singleton object which manages all the SIP accounts. When its
    start method is called, it will load all the accounts from the
    configuration. It is also used to manage the default account (the one
    used for outbound sessions) using the default_account attribute:

    manager = AccountManager()
    manager.default_account = manager.get_account('alice@example.net')

    The following notifications are sent:
     * SIPAccountManagerDidRemoveAccount
     * SIPAccountManagerDidAddAccount
     * SIPAccountManagerDidChangeDefaultAccount
    """

    __metaclass__ = Singleton

    implements(IObserver)

    def __init__(self):
        self.accounts = {}

    def load_accounts(self):
        """
        Load all accounts from the configuration. The accounts will not be
        started until the start method is called.
        """
        configuration = ConfigurationManager()
        bonjour_account = BonjourAccount()
        notification_center = NotificationCenter()
        self.accounts[bonjour_account.id] = bonjour_account
        notification_center.add_observer(self, sender=bonjour_account, name='CFGSettingsObjectDidChange')
        notification_center.post_notification('SIPAccountManagerDidAddAccount', sender=self, data=TimestampedNotificationData(account=bonjour_account))
        # and the other accounts
        names = configuration.get_names(Account.__group__)
        [Account(id) for id in names if id != bonjour_account.id]
        default_account = self.default_account
        if default_account is None or not default_account.enabled:
            try:
                self.default_account = (account for account in self.accounts.itervalues() if account.enabled).next()
            except StopIteration:
                self.default_account = None

    def start(self):
        """
        Start the accounts, which will determine the ones with the enabled flag
        set to activate.
        """
        notification_center = NotificationCenter()
        notification_center.post_notification('SIPAccountManagerWillStart', sender=self, data=TimestampedNotificationData())
        for account in self.accounts.itervalues():
            account.start()
        notification_center.post_notification('SIPAccountManagerDidStart', sender=self, data=TimestampedNotificationData())

    def stop(self):
        """
        Stop the accounts, which will determine the ones that were enabled to
        deactivate. This method returns only once the accounts were stopped
        successfully or they timed out trying.
        """
        notification_center = NotificationCenter()
        notification_center.post_notification('SIPAccountManagerWillEnd', sender=self, data=TimestampedNotificationData())
        procs = [proc.spawn(account.stop) for account in self.accounts.itervalues()]
        proc.waitall(procs)
        notification_center.post_notification('SIPAccountManagerDidEnd', sender=self, data=TimestampedNotificationData())

    def has_account(self, id):
        return id in self.accounts

    def get_account(self, id):
        return self.accounts[id]

    def get_accounts(self):
        return self.accounts.values()

    def iter_accounts(self):
        return self.accounts.itervalues()

    def find_account(self, contact_uri):
        # compare contact_address with account contact
        exact_matches = (account for account in self.accounts.itervalues() if account.enabled and account.contact.username==contact_uri.user)
        # compare username in contact URI with account username
        loose_matches = (account for account in self.accounts.itervalues() if account.enabled and account.id.username==contact_uri.user)
        return chain(exact_matches, loose_matches, [None]).next()

    def handle_notification(self, notification):
        handler = getattr(self, '_NH_%s' % notification.name, Null)
        handler(notification)

    def _NH_CFGSettingsObjectDidChange(self, notification):
        if isinstance(notification.sender, (Account, BonjourAccount)):
            account = notification.sender
            if 'enabled' in notification.data.modified:
                if account.enabled and self.default_account is None:
                    self.default_account = account
                elif not account.enabled and self.default_account is account:
                    try:
                        self.default_account = (account for account in self.accounts.itervalues() if account.enabled).next()
                    except StopIteration:
                        self.default_account = None

    def _NH_CFGSettingsObjectDidChangeID(self, notification):
        self.accounts[notification.data.new_id] = self.accounts.pop(notification.data.old_id)

    def _internal_add_account(self, account):
        """
        This method must only be used by Account object when instantiated.
        """
        self.accounts[account.id] = account
        notification_center = NotificationCenter()
        notification_center.add_observer(self, sender=account, name='CFGSettingsObjectDidChange')
        notification_center.add_observer(self, sender=account, name='CFGSettingsObjectDidChangeID')
        notification_center.post_notification('SIPAccountManagerDidAddAccount', sender=self, data=TimestampedNotificationData(account=account))

    def _internal_remove_account(self, account):
        """
        This method must only be used by Account objects when deleted.
        """
        del self.accounts[account.id]
        notification_center = NotificationCenter()
        notification_center.remove_observer(self, sender=account, name='CFGSettingsObjectDidChange')
        notification_center.remove_observer(self, sender=account, name='CFGSettingsObjectDidChangeID')
        notification_center.post_notification('SIPAccountManagerDidRemoveAccount', sender=self, data=TimestampedNotificationData(account=account))

    def _get_default_account(self):
        settings = SIPSimpleSettings()
        return self.accounts.get(settings.default_account, None)

    def _set_default_account(self, account):
        if account is not None and not account.enabled:
            raise ValueError("account %s is not enabled" % account.id)
        settings = SIPSimpleSettings()
        old_account = self.accounts.get(settings.default_account, None)
        if account is old_account:
            return
        if account is None:
            settings.default_account = None
        else:
            settings.default_account = account.id
        settings.save()
        notification_center = NotificationCenter()
        notification_center.post_notification('SIPAccountManagerDidChangeDefaultAccount', sender=self, data=TimestampedNotificationData(old_account=old_account, account=account))

    default_account = property(_get_default_account, _set_default_account)
    del _get_default_account, _set_default_account


