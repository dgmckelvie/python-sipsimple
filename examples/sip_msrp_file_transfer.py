#!/usr/bin/env python

import sys
sys.path.append(".")
sys.path.append("..")
import re
import traceback
import string
import random
import socket
import os
from thread import start_new_thread
from threading import Thread
from Queue import Queue
from optparse import OptionParser, OptionValueError
from cStringIO import StringIO
import dns.resolver
from dns.exception import DNSException
from application.system import default_host_ip
from application.process import process
from application.configuration import *
import msrp_protocol
from digest import process_www_authenticate
from pypjua import *

re_ip_port = re.compile("^(?P<host>\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(:(?P<port>\d+))?$")
class SIPProxyAddress(tuple):
    def __new__(typ, value):
        match = re_ip_port.search(value)
        if match is None:
            raise ValueError("invalid IP address/port: %r" % value)
        if match.group("port") is None:
            port = 5060
        else:
            port = match.group("port")
            if port > 65535:
                raise ValueError("port is out of range: %d" % port)
        return match.group("host"), port


class Config(ConfigSection):
    _datatypes = {"username": str, "domain": str, "password": str, "outbound_proxy": SIPProxyAddress}
    username = None
    domain = None
    password = None
    outbound_proxy = None, None

process._system_config_directory = os.path.expanduser("~")
configuration = ConfigFile("pypjua.ini")
configuration.read_settings("Account", Config)

_re_msrp = re.compile("^(?P<pre>.*?)MSRP (?P<transaction_id>[a-zA-Z0-9.\-+%=]+) ((?P<method>[A-Z]+)|((?P<code>[0-9]{3})( (?P<comment>.*?))?))\r\n(?P<headers>.*?)\r\n(\r\n(?P<body>.*?)\r\n)?-------\\2(?P<continuation>[$#+])\r\n(?P<post>.*)$", re.DOTALL)

def random_string(length):
    return "".join(random.choice(string.letters + string.digits) for i in xrange(length))

class MSRPFileTransfer(Thread):

    def __init__(self, is_incoming, do_dump, relay_ip, relay_port, relay_username, relay_password, do_srv, fd):
        self.is_incoming = is_incoming
        self.do_dump = do_dump
        self.relay_data = (relay_ip, relay_port, relay_username, relay_password)
        if not all(self.relay_data):
            self.relay_data = None
        self.do_srv = do_srv
        self.local_uri_path = [msrp_protocol.URI(host=default_host_ip, port=12345, session_id=random_string(12))]
        self.remote_uri_path = None
        self.sock = None
        self.msg_id = 1
        self.buf = StringIO()
        self.use_tls = False
        if not is_incoming:
            self.fd = fd
            if self.relay_data is not None:
                self._init_relay()
        Thread.__init__(self)

    def _init_relay(self):
        print "Reserving session at MSRP relay..."
        self.use_tls = True
        relay_ip, relay_port, relay_username, relay_password = self.relay_data
        real_relay_ip = relay_ip
        if self.do_srv:
            try:
                answers = dns.resolver.query("_msrps._tcp.%s" % relay_ip, "SRV")
                real_relay_ip = str(answers[0].target).rstrip(".")
                relay_port = answers[0].port
            except DNSException:
                print "SRV lookup failed, trying normal A record lookup..."
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect((real_relay_ip, relay_port))
        self.sock.settimeout(None)
        self.ssl = socket.ssl(self.sock)
        msrpdata = msrp_protocol.MSRPData(method="AUTH", transaction_id=random_string(12))
        relay_uri = msrp_protocol.URI(host=relay_ip, port=relay_port, use_tls=True)
        msrpdata.add_header(msrp_protocol.ToPathHeader([relay_uri]))
        msrpdata.add_header(msrp_protocol.FromPathHeader(self.local_uri_path))
        self._send(msrpdata.encode(), False)
        data = self._recv_msrp(False)
        if int(data["code"]) != 401:
            raise RuntimeError("Expected 401 response from relay")
        headers = dict(header.split(": ", 1) for header in data["headers"].split("\r\n"))
        www_authenticate = msrp_protocol.WWWAuthenticateHeader(headers["WWW-Authenticate"])
        auth, rsp_auth = process_www_authenticate(relay_username, relay_password, "AUTH", str(relay_uri), **www_authenticate.decoded)
        msrpdata.transaction_id = random_string(12)
        msrpdata.add_header(msrp_protocol.AuthorizationHeader(auth))
        self._send(msrpdata.encode(), False)
        data = self._recv_msrp(False)
        if int(data["code"]) != 200:
            raise RuntimeError("Failed to log on to MSRP relay: %(code)s %(comment)s" % data)
        headers = dict(header.split(": ", 1) for header in data["headers"].split("\r\n"))
        use_path = msrp_protocol.UsePathHeader(headers["Use-Path"]).decoded[0]
        self.local_uri_path = [use_path, self.local_uri_path[0]]
        print 'Reserved session at MSRP relay %s:%d, Use-Path %s' % (real_relay_ip, relay_port, use_path)

    def _send(self, data, queue_print=True):
        global queue
        if self.do_dump:
            msg = "Sending MSRP data:\n%s" % data
            if queue_print:
                queue.put(("print", msg))
            else:
                print msg
        if self.use_tls:
            self.ssl.write(data)
        else:
            self.sock.send(data)

    def _recv(self, queue_print=True):
        global queue
        if self.use_tls:
            try:
                data = self.ssl.read(16384)
            except:
                return ""
        else:
            data = self.sock.recv(16384)
        if self.do_dump:
            msg = "Received MSRP data:\n%s" % data
            if queue_print:
                queue.put(("print", msg))
            else:
                print msg
        return data

    def _recv_msrp(self, queue_print=True):
        global _re_msrp
        while True:
            match = _re_msrp.match(self.buf.getvalue())
            if match is not None:
                self.buf = StringIO()
                self.buf.write(match.group("post"))
                return match.groupdict()
            else:
                data = self._recv(queue_print)
                if len(data) == 0:
                    return None
                self.buf.write(data)

    def set_remote_uri(self, uri_path):
        self.remote_uri_path = [msrp_protocol.parse_uri(uri) for uri in uri_path]
        if self.is_incoming:
            if self.relay_data is not None:
                self._init_relay()
            else:
                self.listen_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.listen_sock.bind(("", 0))
                self.local_uri_path[-1].port = self.listen_sock.getsockname()[1]
        else:
            if self.relay_data is None:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(5)
                self.sock.connect((self.remote_uri_path[0].host, self.remote_uri_path[0].port or 2855))
                print "Connected to remote MSRP party at %s:%d" % self.sock.getpeername()
                self.sock.settimeout(None)
                if self.remote_uri_path[0].use_tls:
                    self.use_tls = True
                    self.ssl = socket.ssl(self.sock)
        self.start()

    def run(self):
        global queue
        if self.is_incoming:
            if self.relay_data is None:
                queue.put(("print", "Listening for MSRP connection on %s:%d" % self.listen_sock.getsockname()))
                self.listen_sock.listen(1)
                self.sock, addr = self.listen_sock.accept()
                self.listen_sock.close()
                del self.listen_sock
                queue.put(("print", "Received incoming MSRP connection from %s:%d" % self.sock.getpeername()))
            queue.put(("print", "Waiting for MSRP file transfer from remote party..."))
            data = self._recv_msrp()
            if data is None:
                queue.put(("print", "MSRP session got disconnected before file transfer was completed."))
                queue.put(("end", None))
            elif data["method"] == "SEND":
                queue.put(("print", "Got MSRP SEND request"))
                response = msrp_protocol.MSRPData(transaction_id=data["transaction_id"], code=200, comment="OK")
                response.add_header(msrp_protocol.ToPathHeader(self.local_uri_path[:-1] + self.remote_uri_path))
                response.add_header(msrp_protocol.FromPathHeader(self.local_uri_path[-1:]))
                self._send(response.encode())
                headers = {}
                for header in data["headers"].split("\r\n"):
                    try:
                        hname, hval = header.split(": ", 2)
                    except ValueError:
                        pass
                    headers[hname] = msrp_protocol.MSRPHeader(hname, hval)
                try:
                    content_type = headers["Content-Type"].decoded
                    filename = headers["Content-Disposition"].decoded[1]["filename"]
                except:
                    traceback.print_exc()
                    queue.put(("print", "Error in header parsing, this is probably not an MSRP file transfer."))
                    queue.put(("end", None))
                    return
                if content_type != "binary/octet-stream":
                    queue.put(("print", "Remote party is not trying to send us a file."))
                    queue.put(("end", None))
                    return
                open(filename, "wb").write(data["body"])
                queue.put(("print", 'Received file "%s" of %d bytes, disconnecting...' % (filename, len(data["body"]))))
                queue.put(("end", None))
        else:
            queue.put(("print", "Starting file transfer..."))
            file_data = self.fd.read()
            msrpdata = msrp_protocol.MSRPData(method="SEND", transaction_id=random_string(12))
            msrpdata.add_header(msrp_protocol.ToPathHeader(self.local_uri_path[:-1] + self.remote_uri_path))
            msrpdata.add_header(msrp_protocol.FromPathHeader(self.local_uri_path[-1:]))
            msrpdata.add_header(msrp_protocol.MessageIDHeader(str(self.msg_id)))
            msrpdata.add_header(msrp_protocol.FailureReportHeader("no"))
            msrpdata.add_header(msrp_protocol.ByteRangeHeader((1, len(file_data), len(file_data))))
            msrpdata.add_header(msrp_protocol.ContentDispositionHeader(["attachment", {"filename": os.path.split(self.fd.name)[-1]}]))
            msrpdata.add_header(msrp_protocol.ContentTypeHeader("binary/octet-stream"))
            data = msrpdata.encode_start() + file_data + msrpdata.encode_end("$")
            self._send(data)
            self.msg_id += 1
            queue.put(("print", "Sent file, waiting for remote party to close session..."))
            while True:
                data = self._recv_msrp()
                if data is None:
                    return

    def disconnect(self):
        if hasattr(self, "listen_sock"):
            self.listen_sock.close()
        elif self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except:
                pass
        if self._Thread__started:
            self.join()

queue = Queue()
packet_count = 0
start_time = None

def event_handler(event_name, **kwargs):
    global start_time, packet_count, queue
    if event_name == "siptrace":
        if start_time is None:
            start_time = kwargs["timestamp"]
        packet_count += 1
        if kwargs["received"]:
            direction = "RECEIVED"
        else:
            direction = "SENDING"
        buf = ["%s: Packet %d, +%s" % (direction, packet_count, (kwargs["timestamp"] - start_time))]
        buf.append("%(timestamp)s: %(source_ip)s:%(source_port)d --> %(destination_ip)s:%(destination_port)d" % kwargs)
        buf.append(kwargs["data"])
        queue.put(("print", "\n".join(buf)))
    elif event_name != "log":
        queue.put(("pypjua_event", (event_name, kwargs)))

def user_input():
    global queue
    while True:
        try:
            msg = raw_input()
            queue.put(("user_input", msg))
        except EOFError:
            queue.put(("end", None))
            break
        except:
            traceback.print_exc()
            queue.put(("end", None))
            break

def do_invite(username, domain, password, proxy_ip, proxy_port, target_username, target_domain, dump_msrp, use_msrp_relay, auto_msrp_relay, msrp_relay_ip, msrp_relay_port, fd, do_siptrace):
    msrp = None
    inv = None
    printed = False
    e = Engine(event_handler, do_siptrace=do_siptrace, auto_sound=False)
    e.start()
    try:
        if proxy_ip is None:
            route = None
        else:
            route = Route(proxy_ip, proxy_port)
        if target_username is None:
            reg = Registration(Credentials(SIPURI(user=username, host=domain), password), route=route)
            reg.register()
        else:
            queue.put(("pypjua_event", ("Registration_state", dict(state="registered"))))
        if target_username is not None:
            inv = Invitation(Credentials(SIPURI(user=username, host=domain), password), SIPURI(user=target_username, host=target_domain), route=route)
    except:
        e.stop()
        raise
    start_new_thread(user_input, ())
    while True:
        try:
            command, data = queue.get()
            if command == "print":
                print data
            if command == "pypjua_event":
                event_name, args = data
                if event_name == "Registration_state":
                    if args["state"] == "registered":
                        if not printed:
                            if target_username is None:
                                print "Registered with SIP address: %s@%s, waiting for incoming session..." % (username, domain)
                            printed = True
                        if inv is not None and inv.state == "DISCONNECTED":
                            if use_msrp_relay:
                                if auto_msrp_relay:
                                    msrp = MSRPFileTransfer(False, dump_msrp, domain, 2855, username, password, True, fd)
                                else:
                                    msrp = MSRPFileTransfer(False, dump_msrp, msrp_relay_ip, msrp_relay_port, username, password, False, fd)
                            else:
                                msrp = MSRPFileTransfer(False, dump_msrp, None, None, username, password, False, fd)
                            inv.invite([MediaStream("message", [str(uri) for uri in msrp.local_uri_path], ["binary/octet-stream"])])
                    elif args["state"] == "unregistered":
                        if args["code"] / 100 != 2:
                            print "Unregistered: %(code)d %(reason)s" % args
                        command = "quit"
                elif event_name == "Invitation_state":
                    if args["state"] == "INCOMING":
                        print "Incoming session..."
                        if inv is None:
                            if args.has_key("streams") and len(args["streams"]) == 1:
                                msrp_stream = args["streams"].pop()
                                if msrp_stream.media_type == "message" and msrp_stream.remote_info[1] == ["binary/octet-stream"]:
                                    inv = args["obj"]
                                    print 'Incoming INVITE from "%s", do you want to accept? (y/n)' % inv.caller_uri.as_str()
                                else:
                                    args["obj"].end()
                            else:
                                args["obj"].end()
                        else:
                            print "Rejecting."
                            args["obj"].end()
                    elif args["state"] == "ESTABLISHED":
                        remote_uri_path = args["streams"].pop().remote_info[0]
                        print "Session negotiated to: %s" % " ".join(remote_uri_path)
                        if target_username is not None:
                            try:
                                msrp.set_remote_uri(remote_uri_path)
                            except:
                                traceback.print_exc()
                                command = "end"
                    elif args["state"] == "DISCONNECTED":
                        if args["obj"] is inv:
                            if args.has_key("code") and args["code"] / 100 != 2:
                                print "Session ended: %(code)d %(reason)s" % args
                            else:
                                print "Session ended"
                            command = "unregister"
            if command == "user_input":
                if inv is not None and inv.state == "INCOMING":
                    if data[0].lower() == "n":
                        command = "end"
                    elif data[0].lower() == "y":
                        try:
                            if use_msrp_relay:
                                if auto_msrp_relay:
                                    msrp = MSRPFileTransfer(True, dump_msrp, domain, 2855, username, password, True, fd)
                                else:
                                    msrp = MSRPFileTransfer(True, dump_msrp, msrp_relay_ip, msrp_relay_port, username, password, False, fd)
                            else:
                                msrp = MSRPFileTransfer(True, dump_msrp, None, None, username, password, False, fd)
                        except RuntimeError:
                            traceback.print_exc()
                            command = "end"
                        else:
                            msrp_stream = inv.proposed_streams.pop()
                            try:
                                msrp.set_remote_uri(msrp_stream.remote_info[0])
                            except:
                                traceback.print_exc()
                                command = "end"
                            else:
                                msrp_stream.set_local_info([str(uri) for uri in msrp.local_uri_path], ["binary/octet-stream"])
                                inv.accept([msrp_stream])
            if command == "end":
                try:
                    if msrp is not None:
                        msrp.disconnect()
                    inv.end()
                except:
                    command = "unregister"
            if command == "unregister":
                if target_username is None:
                    reg.unregister()
                else:
                    command = "quit"
            if command == "quit":
                if msrp is not None:
                    msrp.disconnect()
                e.stop()
                sys.exit()
        except KeyboardInterrupt:
            print "Interrupted, exiting instantly!"
            if msrp is not None:
                msrp.disconnect()
            e.stop()
            sys.exit()
        except Exception:
            traceback.print_exc()
            if msrp is not None:
                msrp.disconnect()
            e.stop()
            sys.exit()

re_host_port = re.compile("^((?P<ip>\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})|(?P<host>[a-zA-Z0-9\-\.]+))(:(?P<port>\d+))?$")
def parse_host_port(option, opt_str, value, parser, host_name, port_name, default_port, allow_host):
    match = re_host_port.match(value)
    if match is None:
        raise OptionValueError("Could not parse supplied address: %s" % value)
    if match.group("ip") is None:
        if allow_host:
            setattr(parser.values, host_name, match.group("host"))
        else:
            raise OptionValueError("Not a IP address: %s" % match.group("host"))
    else:
        setattr(parser.values, host_name, match.group("ip"))
    if match.group("port") is None:
        setattr(parser.values, port_name, default_port)
    else:
        setattr(parser.values, port_name, int(match.group("port")))

def parse_options():
    retval = {}
    description = "This example script will REGISTER using the specified credentials and either sit idle waiting for an incoming MSRP file transfer, or attempt to send the specified file over MSRP to the specified target. The program will close the session and quit when the file transfer is done or CTRL+D is pressed."
    usage = "%prog [options] [target-user@target-domain.com file]"
    default_options = dict(proxy_ip=Config.outbound_proxy[0], proxy_port=Config.outbound_proxy[1], username=Config.username, password=Config.password, domain=Config.domain, dump_msrp=False, msrp_relay_ip=None, msrp_relay_port=None, do_siptrace=False)
    parser = OptionParser(usage=usage, description=description)
    parser.print_usage = parser.print_help
    parser.set_defaults(**default_options)
    parser.add_option("-u", "--username", type="string", dest="username", help="Username to use for the local account. This overrides the setting from the config file.")
    parser.add_option("-d", "--domain", type="string", dest="domain", help="SIP domain to use for the local account. This overrides the setting from the config file.")
    parser.add_option("-p", "--password", type="string", dest="password", help="Password to use to authenticate the local account. This overrides the setting from the config file.")
    parser.add_option("-o", "--outbound-proxy", type="string", action="callback", callback=lambda option, opt_str, value, parser: parse_host_port(option, opt_str, value, parser, "proxy_ip", "proxy_port", 5060), help="Outbound SIP proxy to use. By default a lookup is performed based on SRV and A records. This overrides the setting from the config file.", metavar="IP[:PORT]")
    parser.add_option("-m", "--trace-msrp", action="store_true", dest="dump_msrp", help="Dump the raw contents of incoming and outgoing MSRP messages (disabled by default).")
    parser.add_option("-s", "--trace-sip", action="store_true", dest="do_siptrace", help="Dump the raw contents of incoming and outgoing SIP messages (disabled by default).")
    parser.add_option("-r", "--msrp-relay", type="string", action="callback", callback=lambda option, opt_str, value, parser: parse_host_port(option, opt_str, value, parser, "msrp_relay_ip", "msrp_relay_port", 2855, True), help='MSRP relay to use. By default the MSRP relay will be discovered through the domain part of the SIP URI using SRV records. Use this option with "none" as argument will disable using a MSRP relay', metavar="IP[:PORT]")
    options, args = parser.parse_args()
    if args:
        try:
            target, filename = args
        except ValueError:
            parser.print_usage()
            sys.exit()
        try:
            retval["target_username"], retval["target_domain"] = args[0].split("@")
        except ValueError:
            retval["target_username"], retval["target_domain"] = args[0], options.domain
        try:
            retval["fd"] = open(filename, "rb")
        except IOError, e:
            parser.error(e)
    else:
        retval["target_username"], retval["target_domain"], retval["fd"] = None, None, None
    if not all([options.username, options.domain, options.password]):
        raise RuntimeError("No complete set of SIP credentials specified in config file and on commandline.")
    retval["auto_msrp_relay"] = options.msrp_relay_ip is None
    if retval["auto_msrp_relay"]:
        retval["use_msrp_relay"] = True
    else:
        retval["use_msrp_relay"] = options.msrp_relay_ip.lower() != "none"
    for attr in default_options:
        retval[attr] = getattr(options, attr)
    return retval

def main():
    do_invite(**parse_options())

if __name__ == "__main__":
    main()
