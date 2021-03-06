# Copyright (C) 2008-2010 AG Projects. See LICENSE for details.
#

# python imports

from errno import EADDRINUSE


# classes

cdef class RTPTransport:
    def __cinit__(self, *args, **kwargs):
        cdef pj_pool_t *pool
        cdef pjsip_endpoint *endpoint
        cdef PJSIPUA ua

        ua = _get_ua()
        endpoint = ua._pjsip_endpoint._obj
        pool_name = "RTPTransport_%d" % id(self)

        self.weakref = weakref.ref(self)
        Py_INCREF(self.weakref)

        self._af = pj_AF_INET()
        pj_mutex_create_recursive(ua._pjsip_endpoint._pool, "rtp_transport_lock", &self._lock)
        with nogil:
            pool = pjsip_endpt_create_pool(endpoint, pool_name, 4096, 4096)
        if pool == NULL:
            raise SIPCoreError("Could not allocate memory pool")
        self._pool = pool
        self.state = "NULL"

    def __init__(self, local_rtp_address=None, use_srtp=False, srtp_forced=False, use_ice=False,
                 ice_stun_address=None, ice_stun_port=PJ_STUN_PORT):
        cdef PJSIPUA ua = _get_ua()

        if self.state != "NULL":
            raise SIPCoreError("RTPTransport.__init__() was already called")
        if local_rtp_address is not None and not _is_valid_ip(self._af, local_rtp_address):
            raise ValueError("Not a valid IPv4 address: %s" % local_rtp_address)
        if ice_stun_address is not None and not _is_valid_ip(self._af, ice_stun_address):
            raise ValueError("Not a valid IPv4 address: %s" % ice_stun_address)
        self._local_rtp_addr = local_rtp_address
        self.use_srtp = use_srtp
        self.srtp_forced = srtp_forced
        self.use_ice = use_ice
        self.ice_stun_address = ice_stun_address
        self.ice_stun_port = ice_stun_port

    def __dealloc__(self):
        cdef PJSIPUA ua
        cdef pjsip_endpoint *endpoint
        cdef pjmedia_transport *transport
        cdef pjmedia_transport *wrapped_transport
        cdef pj_pool_t *pool
        cdef Timer timer

        try:
            ua = _get_ua()
        except SIPCoreError:
            return
        endpoint = ua._pjsip_endpoint._obj
        pool = self._pool
        transport = self._obj
        wrapped_transport = self._wrapped_transport

        if self.state in ["LOCAL", "ESTABLISHED"]:
            with nogil:
                pjmedia_transport_media_stop(transport)
        if self._obj != NULL:
            with nogil:
                pjmedia_transport_close(transport)
            if self._obj.type == PJMEDIA_TRANSPORT_TYPE_ICE:
                (<void **> (self._obj.name + 1))[0] = NULL
            if self._wrapped_transport != NULL:
                if self._wrapped_transport.type == PJMEDIA_TRANSPORT_TYPE_ICE:
                    (<void **> (self._obj.name + 1))[0] = NULL
                self._wrapped_transport = NULL
            self._obj = NULL
        if self._wrapped_transport != NULL:
            if self._wrapped_transport.type == PJMEDIA_TRANSPORT_TYPE_ICE:
                (<void **> (self._obj.name + 1))[0] = NULL
            with nogil:
                pjmedia_transport_close(wrapped_transport)
        if self._pool != NULL:
            with nogil:
                pjsip_endpt_release_pool(endpoint, pool)

        timer = Timer()
        try:
            timer.schedule(60, deallocate_weakref, self.weakref)
        except SIPCoreError:
            pass

    cdef PJSIPUA _check_ua(self):
        cdef PJSIPUA ua
        try:
            ua = _get_ua()
            return ua
        except:
            self.state = "INVALID"
            self._obj = NULL
            self._wrapped_transport = NULL
            self._pool = NULL
            return None

    cdef int _get_info(self, pjmedia_transport_info *info) except -1:
        cdef int status
        cdef pjmedia_transport *transport

        transport = self._obj

        with nogil:
            pjmedia_transport_info_init(info)
            status = pjmedia_transport_get_info(transport, info)
        if status != 0:
            raise PJSIPError("Could not get transport info", status)
        return 0

    property local_rtp_port:

        def __get__(self):
            cdef int status
            cdef pj_mutex_t *lock = self._lock
            cdef pjmedia_transport_info info
            cdef PJSIPUA ua

            ua = self._check_ua()
            if ua is None:
                return None

            with nogil:
                status = pj_mutex_lock(lock)
            if status != 0:
                raise PJSIPError("failed to acquire lock", status)
            try:
                if self.state in ["NULL", "WAIT_STUN", "INVALID"]:
                    return None
                self._get_info(&info)
                if info.sock_info.rtp_addr_name.addr.sa_family != 0:
                    return pj_sockaddr_get_port(&info.sock_info.rtp_addr_name)
                else:
                    return None
            finally:
                with nogil:
                    pj_mutex_unlock(lock)

    property local_rtp_address:

        def __get__(self):
            cdef char buf[PJ_INET6_ADDRSTRLEN]
            cdef int status
            cdef pj_mutex_t *lock = self._lock
            cdef pjmedia_transport_info info
            cdef PJSIPUA ua

            ua = self._check_ua()
            if ua is None:
                return self._local_rtp_addr

            with nogil:
                status = pj_mutex_lock(lock)
            if status != 0:
                raise PJSIPError("failed to acquire lock", status)
            try:
                if self.state in ["NULL", "WAIT_STUN", "INVALID"]:
                    return self._local_rtp_addr
                self._get_info(&info)
                if pj_sockaddr_has_addr(&info.sock_info.rtp_addr_name):
                    return pj_sockaddr_print(&info.sock_info.rtp_addr_name, buf, PJ_INET6_ADDRSTRLEN, 0)
                else:
                    return None
            finally:
                with nogil:
                    pj_mutex_unlock(lock)

    property remote_rtp_port_received:

        def __get__(self):
            cdef int status
            cdef pj_mutex_t *lock = self._lock
            cdef pjmedia_transport_info info
            cdef PJSIPUA ua

            ua = self._check_ua()
            if ua is None:
                return None

            with nogil:
                status = pj_mutex_lock(lock)
            if status != 0:
                raise PJSIPError("failed to acquire lock", status)
            try:
                if self.state in ["NULL", "WAIT_STUN", "INVALID"]:
                    return None
                if self._ice_active:
                    return self.remote_rtp_port_ice
                self._get_info(&info)
                if info.src_rtp_name.addr.sa_family != 0:
                    return pj_sockaddr_get_port(&info.src_rtp_name)
                else:
                    return None
            finally:
                with nogil:
                    pj_mutex_unlock(lock)

    property remote_rtp_address_received:

        def __get__(self):
            cdef char buf[PJ_INET6_ADDRSTRLEN]
            cdef int status
            cdef pj_mutex_t *lock = self._lock
            cdef pjmedia_transport_info info
            cdef PJSIPUA ua

            ua = self._check_ua()
            if ua is None:
                return None

            with nogil:
                status = pj_mutex_lock(lock)
            if status != 0:
                raise PJSIPError("failed to acquire lock", status)
            try:
                if self.state in ["NULL", "WAIT_STUN", "INVALID"]:
                    return None
                if self._ice_active:
                    return self.remote_rtp_address_ice
                self._get_info(&info)
                if pj_sockaddr_has_addr(&info.src_rtp_name):
                    return pj_sockaddr_print(&info.src_rtp_name, buf, PJ_INET6_ADDRSTRLEN, 0)
                else:
                    return None
            finally:
                with nogil:
                    pj_mutex_unlock(lock)

    property srtp_active:

        def __get__(self):
            cdef int i
            cdef int status
            cdef pj_mutex_t *lock = self._lock
            cdef pjmedia_srtp_info *srtp_info
            cdef pjmedia_transport_info info
            cdef PJSIPUA ua

            ua = self._check_ua()
            if ua is None:
                return False

            with nogil:
                status = pj_mutex_lock(lock)
            if status != 0:
                raise PJSIPError("failed to acquire lock", status)
            try:
                if self.state in ["NULL", "WAIT_STUN", "INVALID"]:
                    return False
                self._get_info(&info)
                for i from 0 <= i < info.specific_info_cnt:
                    if info.spc_info[i].type == PJMEDIA_TRANSPORT_TYPE_SRTP:
                        srtp_info = <pjmedia_srtp_info *> info.spc_info[i].buffer
                        return bool(srtp_info.active)
                return False
            finally:
                with nogil:
                    pj_mutex_unlock(lock)

    property ice_active:

        def __get__(self):
            return bool(self._ice_active)

    property local_rtp_candidate_type:

        def __get__(self):
            return self._local_rtp_candidate_type if self._ice_active else None

    property remote_rtp_candidate_type:

        def __get__(self):
            return self._remote_rtp_candidate_type if self._ice_active else None

    cdef int _update_local_sdp(self, SDPSession local_sdp, int sdp_index, pjmedia_sdp_session *remote_sdp) except -1:
        cdef int status
        cdef pj_pool_t *pool
        cdef pjmedia_sdp_session *pj_local_sdp
        cdef pjmedia_transport *transport

        pj_local_sdp = local_sdp.get_sdp_session()
        pool = self._pool
        transport = self._obj

        if sdp_index < 0:
            raise ValueError("sdp_index argument cannot be negative")
        if sdp_index >= local_sdp.get_sdp_session().media_count:
            raise ValueError("sdp_index argument out of range")
        with nogil:
            status = pjmedia_transport_media_create(transport, pool, 0, remote_sdp, sdp_index)
        if status != 0:
            raise PJSIPError("Could not create media transport", status)
        with nogil:
            status = pjmedia_transport_encode_sdp(transport, pool, pj_local_sdp, remote_sdp, sdp_index)
        if status != 0:
            raise PJSIPError("Could not update SDP for media transport", status)
        local_sdp._update()
        return 0

    def set_LOCAL(self, SDPSession local_sdp, int sdp_index):
        cdef int status
        cdef pj_mutex_t *lock = self._lock

        _get_ua()

        with nogil:
            status = pj_mutex_lock(lock)
        if status != 0:
            raise PJSIPError("failed to acquire lock", status)
        try:
            if local_sdp is None:
                raise SIPCoreError("local_sdp argument cannot be None")
            if self.state == "LOCAL":
                return
            if self.state != "INIT":
                raise SIPCoreError('set_LOCAL can only be called in the "INIT" state, current state is "%s"' % self.state)
            self._update_local_sdp(local_sdp, sdp_index, NULL)
            self.state = "LOCAL"
        finally:
            with nogil:
                pj_mutex_unlock(lock)

    def set_ESTABLISHED(self, BaseSDPSession local_sdp, BaseSDPSession remote_sdp, int sdp_index):
        cdef int status
        cdef pj_mutex_t *lock = self._lock
        cdef pjmedia_sdp_session *pj_local_sdp
        cdef pjmedia_sdp_session *pj_remote_sdp
        cdef pjmedia_transport *transport = self._obj

        _get_ua()

        with nogil:
            status = pj_mutex_lock(lock)
        if status != 0:
            raise PJSIPError("failed to acquire lock", status)
        try:
            transport = self._obj

            if None in [local_sdp, remote_sdp]:
                raise SIPCoreError("SDP arguments cannot be None")
            pj_local_sdp = local_sdp.get_sdp_session()
            pj_remote_sdp = remote_sdp.get_sdp_session()
            if self.state == "ESTABLISHED":
                return
            if self.state not in ["INIT", "LOCAL"]:
                raise SIPCoreError('set_ESTABLISHED can only be called in the "INIT" and "LOCAL" states, ' +
                                   'current state is "%s"' % self.state)
            if self.state == "INIT":
                if not isinstance(local_sdp, SDPSession):
                    raise TypeError('local_sdp argument should be of type SDPSession when going from the "INIT" to the "ESTABLISHED" state')
                self._update_local_sdp(<SDPSession>local_sdp, sdp_index, pj_remote_sdp)
            with nogil:
                status = pjmedia_transport_media_start(transport, self._pool, pj_local_sdp, pj_remote_sdp, sdp_index)
            if status != 0:
                raise PJSIPError("Could not start media transport", status)
            if remote_sdp.media[sdp_index].connection is None:
                if remote_sdp.connection is not None:
                    self.remote_rtp_address_sdp = remote_sdp.connection.address
            else:
                self.remote_rtp_address_sdp = remote_sdp.media[sdp_index].connection.address
            self.remote_rtp_port_sdp = remote_sdp.media[sdp_index].port
            self.state = "ESTABLISHED"
        finally:
            with nogil:
                pj_mutex_unlock(lock)

    def set_INIT(self):
        global _ice_cb
        cdef int af
        cdef int i
        cdef int status
        cdef int port
        cdef pj_caching_pool *caching_pool
        cdef pj_ice_strans_cfg ice_cfg
        cdef pj_mutex_t *lock = self._lock
        cdef pj_str_t local_ip
        cdef pj_str_t *local_ip_address = &local_ip
        cdef pjmedia_endpt *media_endpoint
        cdef pjmedia_srtp_setting srtp_setting
        cdef pjmedia_transport **transport_address
        cdef pjmedia_transport *wrapped_transport
        cdef pjsip_endpoint *sip_endpoint
        cdef PJSIPUA ua

        ua = _get_ua()

        with nogil:
            status = pj_mutex_lock(lock)
        if status != 0:
            raise PJSIPError("failed to acquire lock", status)
        try:
            af = self._af
            caching_pool = &ua._caching_pool._obj
            media_endpoint = ua._pjmedia_endpoint._obj
            sip_endpoint = ua._pjsip_endpoint._obj
            transport_address = &self._obj

            if self.state == "INIT":
                return
            if self.state in ["LOCAL", "ESTABLISHED"]:
                with nogil:
                    status = pjmedia_transport_media_stop(transport_address[0])
                if status != 0:
                    raise PJSIPError("Could not stop media transport", status)
                self.remote_rtp_address_sdp = None
                self.remote_rtp_port_sdp = None
                self.remote_rtp_address_ice = None
                self.remote_rtp_port_ice = None
                self.state = "INIT"
            elif self.state == "NULL":
                if self._local_rtp_addr is None:
                    local_ip_address = NULL
                else:
                    _str_to_pj_str(self._local_rtp_addr, &local_ip)
                if self.use_ice:
                    with nogil:
                        pj_ice_strans_cfg_default(&ice_cfg)
                    ice_cfg.af = self._af
                    with nogil:
                        pj_stun_config_init(&ice_cfg.stun_cfg, &caching_pool.factory, 0,
                                            pjmedia_endpt_get_ioqueue(media_endpoint),
                                            pjsip_endpt_get_timer_heap(sip_endpoint))
                    if self.ice_stun_address is not None:
                        _str_to_pj_str(self.ice_stun_address, &ice_cfg.stun.server)
                        ice_cfg.stun.port = self.ice_stun_port
                    # IIRC we can't choose the port for ICE
                    with nogil:
                        status = pj_sockaddr_init(ice_cfg.af, &ice_cfg.stun.cfg.bound_addr, local_ip_address, 0)
                    if status != 0:
                        raise PJSIPError("Could not init ICE bound address", status)
                    # The state callback won't be called for the 'ICE Candidates Gathering' state because transport is not
                    # yet initialized, so we fake it. -Saul
                    _add_event("RTPTransportICENegotiationStateDidChange", dict(obj=self, state="ICE Candidates Gathering"))
                    with nogil:
                        status = pjmedia_ice_create2(media_endpoint, NULL, 2, &ice_cfg, &_ice_cb, 0, transport_address)
                    if status != 0:
                        raise PJSIPError("Could not create ICE media transport", status)
                    (<void **> (self._obj.name + 1))[0] = <void *> self.weakref
                else:
                    status = PJ_EBUG
                    for i in xrange(ua._rtp_port_index, ua._rtp_port_index + ua._rtp_port_usable_count, 2):
                        port = ua._rtp_port_start + i % ua._rtp_port_usable_count
                        with nogil:
                            status = pjmedia_transport_udp_create3(media_endpoint, af, NULL, local_ip_address,
                                                                   port, 0, transport_address)
                        if status != PJ_ERRNO_START_SYS + EADDRINUSE:
                            ua._rtp_port_index = (i + 2) % ua._rtp_port_usable_count
                            break
                    if status != 0:
                        raise PJSIPError("Could not create UDP/RTP media transport", status)
                if self.use_srtp:
                    wrapped_transport = self._wrapped_transport = self._obj
                    self._obj = NULL
                    with nogil:
                        pjmedia_srtp_setting_default(&srtp_setting)
                    if self.srtp_forced:
                        srtp_setting.use = PJMEDIA_SRTP_MANDATORY
                    with nogil:
                        status = pjmedia_transport_srtp_create(media_endpoint, wrapped_transport, &srtp_setting, transport_address)
                    if status != 0:
                        with nogil:
                            pjmedia_transport_close(wrapped_transport)
                        self._wrapped_transport = NULL
                        raise PJSIPError("Could not create SRTP media transport", status)
                if not self.use_ice or self.ice_stun_address is None:
                    self.state = "INIT"
                    _add_event("RTPTransportDidInitialize", dict(obj=self))
                else:
                    self.state = "WAIT_STUN"
            else:
                raise SIPCoreError('set_INIT can only be called in the "NULL", "LOCAL" and "ESTABLISHED" states, ' +
                                   'current state is "%s"' % self.state)
        finally:
            with nogil:
                pj_mutex_unlock(lock)


cdef class MediaCheckTimer(Timer):
    def __init__(self, media_check_interval):
        self.media_check_interval = media_check_interval


cdef class AudioTransport:
    def __cinit__(self, *args, **kwargs):
        cdef pj_pool_t *pool
        cdef pjsip_endpoint *endpoint
        cdef PJSIPUA ua

        ua = _get_ua()
        endpoint = ua._pjsip_endpoint._obj
        pool_name = "AudioTransport_%d" % id(self)

        self.weakref = weakref.ref(self)
        Py_INCREF(self.weakref)

        pj_mutex_create_recursive(ua._pjsip_endpoint._pool, "audio_transport_lock", &self._lock)
        with nogil:
            pool = pjsip_endpt_create_pool(endpoint, pool_name, 4096, 4096)
        if pool == NULL:
            raise SIPCoreError("Could not allocate memory pool")
        self._pool = pool
        self._slot = -1
        self._timer = None
        self._volume = 100

    def __init__(self, AudioMixer mixer, RTPTransport transport,
                 BaseSDPSession remote_sdp=None, int sdp_index=0, enable_silence_detection=False, list codecs=None):
        cdef int status
        cdef pj_pool_t *pool
        cdef pjmedia_endpt *media_endpoint
        cdef pjmedia_sdp_media *local_media
        cdef pjmedia_sdp_session *local_sdp_c
        cdef pjmedia_transport_info info
        cdef list global_codecs
        cdef SDPSession local_sdp
        cdef PJSIPUA ua

        ua = _get_ua()
        media_endpoint = ua._pjmedia_endpoint._obj
        pool = self._pool

        if self.transport is not None:
            raise SIPCoreError("AudioTransport.__init__() was already called")
        if mixer is None:
            raise ValueError("mixer argument may not be None")
        if transport is None:
            raise ValueError("transport argument cannot be None")
        if sdp_index < 0:
            raise ValueError("sdp_index argument cannot be negative")
        if transport.state != "INIT":
            raise SIPCoreError('RTPTransport object provided is not in the "INIT" state, but in the "%s" state' %
                               transport.state)
        self._vad = int(bool(enable_silence_detection))
        self.mixer = mixer
        self.transport = transport
        transport._get_info(&info)
        global_codecs = ua._pjmedia_endpoint._get_current_codecs()
        if codecs is None:
            codecs = global_codecs
        try:
            ua._pjmedia_endpoint._set_codecs(codecs, self.mixer.sample_rate)
            with nogil:
                status = pjmedia_endpt_create_sdp(media_endpoint, pool, 1, &info.sock_info, &local_sdp_c)
            if status != 0:
                raise PJSIPError("Could not generate SDP for audio session", status)
        finally:
            ua._pjmedia_endpoint._set_codecs(global_codecs, 32000)
        local_sdp = SDPSession_create(local_sdp_c)
        if remote_sdp is None:
            self._is_offer = 1
            self.transport.set_LOCAL(local_sdp, 0)
        else:
            self._is_offer = 0
            if sdp_index != 0:
                local_sdp.media = (sdp_index+1) * local_sdp.media
            self.transport.set_ESTABLISHED(local_sdp, remote_sdp, sdp_index)
        local_sdp_c = local_sdp.get_sdp_session()
        with nogil:
            local_media = pjmedia_sdp_media_clone(pool, local_sdp_c.media[sdp_index])
        self._local_media = local_media

    def __dealloc__(self):
        cdef PJSIPUA ua
        cdef Timer timer
        try:
            ua = _get_ua()
        except SIPCoreError:
            return
        cdef pjsip_endpoint *endpoint = ua._pjsip_endpoint._obj
        cdef pj_pool_t *pool = self._pool
        if self._obj != NULL:
            self.stop()
        if self._pool != NULL:
            with nogil:
                pjsip_endpt_release_pool(endpoint, pool)

        timer = Timer()
        try:
            timer.schedule(60, deallocate_weakref, self.weakref)
        except SIPCoreError:
            pass

    cdef PJSIPUA _check_ua(self):
        cdef PJSIPUA ua
        try:
            ua = _get_ua()
            return ua
        except:
            self._obj = NULL
            self._pool = NULL
            return None

    property is_active:

        def __get__(self):
            self._check_ua()
            return bool(self._obj != NULL)

    property is_started:

        def __get__(self):
            return bool(self._is_started)

    property codec:

        def __get__(self):
            self._check_ua()
            if self._obj == NULL:
                return None
            else:
                return _pj_str_to_str(self._stream_info.fmt.encoding_name)

    property sample_rate:

        def __get__(self):
            self._check_ua()
            if self._obj == NULL:
                return None
            else:
                return self._stream_info.fmt.clock_rate

    property enable_silence_detection:

        def __get__(self):
            return bool(self._vad)

    property statistics:

        def __get__(self):
            cdef int status
            cdef pj_mutex_t *lock = self._lock
            cdef pjmedia_rtcp_stat stat
            cdef pjmedia_stream *stream
            cdef dict statistics = dict()
            cdef PJSIPUA ua

            ua = self._check_ua()
            if ua is None:
                return None

            with nogil:
                status = pj_mutex_lock(lock)
            if status != 0:
                raise PJSIPError("failed to acquire lock", status)
            try:
                stream = self._obj

                if self._cached_statistics is not None:
                    return self._cached_statistics.copy()
                if self._obj == NULL:
                    return None
                with nogil:
                    status = pjmedia_stream_get_stat(stream, &stat)
                if status != 0:
                    raise PJSIPError("Could not get RTP statistics", status)
                statistics["rtt"] = _pj_math_stat_to_dict(&stat.rtt)
                statistics["rx"] = _pjmedia_rtcp_stream_stat_to_dict(&stat.rx)
                statistics["tx"] = _pjmedia_rtcp_stream_stat_to_dict(&stat.tx)
                return statistics
            finally:
                with nogil:
                    pj_mutex_unlock(lock)

    property volume:

        def __get__(self):
            return self._volume

        def __set__(self, value):
            cdef int slot
            cdef int status
            cdef int volume
            cdef pj_mutex_t *lock = self._lock
            cdef pjmedia_conf *conf_bridge
            cdef PJSIPUA ua

            ua = self._check_ua()

            if ua is not None:
                with nogil:
                    status = pj_mutex_lock(lock)
                if status != 0:
                    raise PJSIPError("failed to acquire lock", status)
            try:
                conf_bridge = self.mixer._obj
                slot = self._slot

                if value < 0:
                    raise ValueError("volume attribute cannot be negative")
                if ua is not None and self._obj != NULL:
                    volume = int(value * 1.28 - 128)
                    with nogil:
                        status = pjmedia_conf_adjust_rx_level(conf_bridge, slot, volume)
                    if status != 0:
                        raise PJSIPError("Could not set volume of audio transport", status)
                self._volume = value
            finally:
                if ua is not None:
                    with nogil:
                        pj_mutex_unlock(lock)

    property slot:

        def __get__(self):
            self._check_ua()
            if self._slot == -1:
                return None
            else:
                return self._slot

    def get_local_media(self, is_offer, direction="sendrecv"):
        cdef int status
        cdef pj_mutex_t *lock = self._lock
        cdef object direction_attr
        cdef SDPAttribute attr
        cdef SDPMediaStream local_media

        _get_ua()

        with nogil:
            status = pj_mutex_lock(lock)
        if status != 0:
            raise PJSIPError("failed to acquire lock", status)
        try:
            if is_offer and direction not in ["sendrecv", "sendonly", "recvonly", "inactive"]:
                raise SIPCoreError("Unknown direction: %s" % direction)
            local_media = SDPMediaStream_create(self._local_media)
            local_media.attributes = [<object> attr for attr in local_media.attributes if attr.name not in ["sendrecv",
                                                                                                            "sendonly",
                                                                                                            "recvonly",
                                                                                                            "inactive"]]
            if is_offer:
                direction_attr = direction
            else:
                if self.direction is None or "recv" in self.direction:
                    direction_attr = "sendrecv"
                else:
                    direction_attr = "sendonly"
            local_media.attributes.append(SDPAttribute(direction_attr, ""))
            for attribute in local_media.attributes:
                if attribute.name == 'rtcp':
                    attribute.value = attribute.value.split(' ', 1)[0]
            return local_media
        finally:
            with nogil:
                pj_mutex_unlock(lock)

    def start(self, BaseSDPSession local_sdp, BaseSDPSession remote_sdp, int sdp_index,
              int no_media_timeout=10, int media_check_interval=30):
        cdef int status
        cdef object desired_state
        cdef pj_mutex_t *lock = self._lock
        cdef pj_pool_t *pool
        cdef pjmedia_endpt *media_endpoint
        cdef pjmedia_port *media_port
        cdef pjmedia_sdp_media *local_media
        cdef pjmedia_sdp_session *pj_local_sdp
        cdef pjmedia_sdp_session *pj_remote_sdp
        cdef pjmedia_stream **stream_address
        cdef pjmedia_stream_info *stream_info_address
        cdef pjmedia_transport *transport
        cdef PJSIPUA ua

        ua = _get_ua()

        with nogil:
            status = pj_mutex_lock(lock)
        if status != 0:
            raise PJSIPError("failed to acquire lock", status)
        try:
            pool = self._pool
            media_endpoint = ua._pjmedia_endpoint._obj
            stream_address = &self._obj
            stream_info_address = &self._stream_info
            transport = self.transport._obj

            if self._is_started:
                raise SIPCoreError("This AudioTransport was already started once")
            desired_state = ("LOCAL" if self._is_offer else "ESTABLISHED")
            if self.transport.state != desired_state:
                raise SIPCoreError('RTPTransport object provided is not in the "%s" state, but in the "%s" state' %
                                   (desired_state, self.transport.state))
            if None in [local_sdp, remote_sdp]:
                raise ValueError("SDP arguments cannot be None")
            pj_local_sdp = local_sdp.get_sdp_session()
            pj_remote_sdp = remote_sdp.get_sdp_session()
            if sdp_index < 0:
                raise ValueError("sdp_index argument cannot be negative")
            if local_sdp.media[sdp_index].port == 0 or remote_sdp.media[sdp_index].port == 0:
                raise SIPCoreError("Cannot start a rejected audio stream")
            if no_media_timeout < 0:
                raise ValueError("no_media_timeout value cannot be negative")
            if media_check_interval < 0:
                raise ValueError("media_check_interval value cannot be negative")
            if self.transport.state == "LOCAL":
                self.transport.set_ESTABLISHED(local_sdp, remote_sdp, sdp_index)
            with nogil:
                status = pjmedia_stream_info_from_sdp(stream_info_address, pool, media_endpoint,
                                                      pj_local_sdp, pj_remote_sdp, sdp_index)
            if status != 0:
                raise PJSIPError("Could not parse SDP for audio session", status)
            if self._stream_info.param == NULL:
                raise SIPCoreError("Could not parse SDP for audio session")
            self._stream_info.param.setting.vad = self._vad
            with nogil:
                status = pjmedia_stream_create(media_endpoint, pool, stream_info_address,
                                               transport, NULL, stream_address)
            if status != 0:
                raise PJSIPError("Could not initialize RTP for audio session", status)
            with nogil:
                status = pjmedia_stream_set_dtmf_callback(stream_address[0], _AudioTransport_cb_dtmf, <void *> self.weakref)
            if status != 0:
                with nogil:
                    pjmedia_stream_destroy(stream_address[0])
                self._obj = NULL
                raise PJSIPError("Could not set DTMF callback for audio session", status)
            with nogil:
                status = pjmedia_stream_start(stream_address[0])
            if status != 0:
                with nogil:
                    pjmedia_stream_destroy(stream_address[0])
                self._obj = NULL
                raise PJSIPError("Could not start RTP for audio session", status)
            with nogil:
                status = pjmedia_stream_get_port(stream_address[0], &media_port)
            if status != 0:
                with nogil:
                    pjmedia_stream_destroy(stream_address[0])
                self._obj = NULL
                raise PJSIPError("Could not get audio port for audio session", status)
            try:
                self._slot = self.mixer._add_port(ua, pool, media_port)
                if self._volume != 100:
                    self.volume = self._volume
            except:
                with nogil:
                    pjmedia_stream_destroy(stream_address[0])
                self._obj = NULL
                raise
            self.direction = "sendrecv"
            self.update_direction(local_sdp.media[sdp_index].direction)
            with nogil:
                local_media = pjmedia_sdp_media_clone(pool, pj_local_sdp.media[sdp_index])
            self._local_media = local_media
            self._is_started = 1
            if no_media_timeout > 0:
                self._timer = MediaCheckTimer(media_check_interval)
                self._timer.schedule(no_media_timeout, <timer_callback>self._cb_check_rtp, self)
        finally:
            with nogil:
                pj_mutex_unlock(lock)

    def stop(self):
        cdef int status
        cdef pj_mutex_t *lock = self._lock
        cdef pjmedia_stream *stream
        cdef PJSIPUA ua

        ua = self._check_ua()

        if ua is not None:
            with nogil:
                status = pj_mutex_lock(lock)
            if status != 0:
                raise PJSIPError("failed to acquire lock", status)
        try:
            stream = self._obj

            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            if self._obj == NULL:
                return
            self.mixer._remove_port(ua, self._slot)
            self._cached_statistics = self.statistics
            with nogil:
                pjmedia_stream_destroy(stream)
            self._obj = NULL
            self.transport.set_INIT()
        finally:
            if ua is not None:
                with nogil:
                    pj_mutex_unlock(lock)

    def update_direction(self, direction):
        cdef int status
        cdef pj_mutex_t *lock = self._lock
        cdef pjmedia_stream *stream

        _get_ua()

        with nogil:
            status = pj_mutex_lock(lock)
        if status != 0:
            raise PJSIPError("failed to acquire lock", status)
        try:
            stream = self._obj

            if self._obj == NULL:
                raise SIPCoreError("Stream is not active")
            if direction not in ["sendrecv", "sendonly", "recvonly", "inactive"]:
                raise SIPCoreError("Unknown direction: %s" % direction)
            if direction == self.direction:
                return
            self.direction = direction
        finally:
            with nogil:
                pj_mutex_unlock(lock)

    def send_dtmf(self, digit):
        cdef int status
        cdef pj_mutex_t *lock = self._lock
        cdef pj_str_t digit_pj
        cdef pjmedia_stream *stream
        cdef PJSIPUA ua

        ua = _get_ua()

        with nogil:
            status = pj_mutex_lock(lock)
        if status != 0:
            raise PJSIPError("failed to acquire lock", status)
        try:
            stream = self._obj

            if self._obj == NULL:
                raise SIPCoreError("Stream is not active")
            if len(digit) != 1 or digit not in "0123456789*#ABCD":
                raise SIPCoreError("Not a valid DTMF digit: %s" % digit)
            _str_to_pj_str(digit, &digit_pj)
            if not self._stream_info.tx_event_pt < 0:
                # If the remote doesn't support telephone-event just don't send DTMF
                with nogil:
                    status = pjmedia_stream_dial_dtmf(stream, &digit_pj)
                if status != 0:
                    raise PJSIPError("Could not send DTMF digit on audio stream", status)
        finally:
            with nogil:
                pj_mutex_unlock(lock)

    cdef int _cb_check_rtp(self, MediaCheckTimer timer) except -1 with gil:
        cdef int status
        cdef pj_mutex_t *lock = self._lock
        cdef pjmedia_rtcp_stat stat
        cdef pjmedia_stream *stream

        with nogil:
            status = pj_mutex_lock(lock)
        if status != 0:
            raise PJSIPError("failed to acquire lock", status)
        try:
            stream = self._obj
            if stream == NULL:
                return 0

            if self._timer is None:
                return 0
            self._timer = None
            with nogil:
                status = pjmedia_stream_get_stat(stream, &stat)
            if status == 0:
                if self._packets_received == stat.rx.pkt and self.direction == "sendrecv":
                    _add_event("RTPAudioTransportDidNotGetRTP", dict(obj=self, got_any=(stat.rx.pkt != 0)))
                self._packets_received = stat.rx.pkt
                if timer.media_check_interval > 0:
                    self._timer = MediaCheckTimer(timer.media_check_interval)
                    self._timer.schedule(timer.media_check_interval, <timer_callback>self._cb_check_rtp, self)
        finally:
            with nogil:
                pj_mutex_unlock(lock)


# helper functions

cdef dict _pj_math_stat_to_dict(pj_math_stat *stat):
    cdef dict retval = dict()
    retval["count"] = stat.n
    retval["max"] = stat.max
    retval["min"] = stat.min
    retval["last"] = stat.last
    retval["avg"] = stat.mean
    return retval

cdef dict _pjmedia_rtcp_stream_stat_to_dict(pjmedia_rtcp_stream_stat *stream_stat):
    cdef dict retval = dict()
    retval["packets"] = stream_stat.pkt
    retval["bytes"] = stream_stat.bytes
    retval["packets_discarded"] = stream_stat.discard
    retval["packets_lost"] = stream_stat.loss
    retval["packets_reordered"] = stream_stat.reorder
    retval["packets_duplicate"] = stream_stat.dup
    retval["loss_period"] = _pj_math_stat_to_dict(&stream_stat.loss_period)
    retval["burst_loss"] = bool(stream_stat.loss_type.burst)
    retval["random_loss"] = bool(stream_stat.loss_type.random)
    retval["jitter"] = _pj_math_stat_to_dict(&stream_stat.jitter)
    return retval

# callback functions

cdef void _RTPTransport_cb_ice_complete(pjmedia_transport *tp, pj_ice_strans_op op, int status) with gil:
    cdef void *rtp_transport_void = NULL
    cdef RTPTransport rtp_transport
    cdef PJSIPUA ua
    try:
        ua = _get_ua()
    except:
        return
    try:
        if tp != NULL:
            rtp_transport_void = (<void **> (tp.name + 1))[0]
        if rtp_transport_void != NULL:
            rtp_transport = (<object> rtp_transport_void)()
            if rtp_transport is None:
                return
            if op == PJ_ICE_STRANS_OP_NEGOTIATION:
                if status == 0:
                    rtp_transport._ice_active = 1
                else:
                    _add_event("RTPTransportICENegotiationDidFail", dict(obj=rtp_transport, reason=_pj_status_to_str(status)))
            elif op == PJ_ICE_STRANS_OP_INIT:
                if status == 0:
                    rtp_transport.state = "INIT"
                else:
                    rtp_transport.state = "INVALID"
                if status == 0:
                    _add_event("RTPTransportDidInitialize", dict(obj=rtp_transport))
                else:
                    _add_event("RTPTransportDidFail", dict(obj=rtp_transport, reason=_pj_status_to_str(status)))
    except:
        ua._handle_exception(1)

cdef void _RTPTransport_cb_ice_candidates_chosen(pjmedia_transport *tp, int status, pj_ice_candidate_pair rtp_pair, pj_ice_candidate_pair rtcp_pair, char *duration, char *local_candidates, char *remote_candidates, char *valid_list) with gil:
    cdef void *rtp_transport_void = NULL
    cdef RTPTransport rtp_transport
    cdef PJSIPUA ua
    cdef dict chosen_local_candidates
    cdef dict chosen_remote_candidates
    cdef list local_cand_list = list()
    cdef list remote_cand_list = list()
    cdef list v_list = list()
    try:
        ua = _get_ua()
    except:
        return
    try:
        if tp != NULL:
            rtp_transport_void = (<void **> (tp.name + 1))[0]
        if rtp_transport_void != NULL:
            rtp_transport = (<object> rtp_transport_void)()
            if rtp_transport is None:
                return
            if status == 0:
                for item in local_candidates.split("\r\n"):
                    if item:
                        item_id, component_id, address, comp_type = item.split(" ")
                        local_cand_list.append([item_id, component_id, address, comp_type])
                for item in remote_candidates.split("\r\n"):
                    if item:
                        item_id, component_id, address, comp_type = item.split(" ")
                        remote_cand_list.append([item_id, component_id, address, comp_type])
                for item in valid_list.split("\r\n"):
                    if item:
                        item_id, component_id, source, destination, nomination, state = item.split(" ")
                        v_list.append([item_id, component_id, source, destination, nomination, state])
                chosen_local_candidates = dict(rtp_cand_type=rtp_pair.local_type, rtp_cand_ip=rtp_pair.local_ip, rtcp_cand_type=rtcp_pair.local_type, rtcp_cand_ip=rtcp_pair.local_ip)
                chosen_remote_candidates = dict(rtp_cand_type=rtp_pair.remote_type, rtp_cand_ip=rtp_pair.remote_ip, rtcp_cand_type=rtcp_pair.remote_type, rtcp_cand_ip=rtcp_pair.remote_ip)
                rtp_transport.remote_rtp_address_ice, rtp_transport.remote_rtp_port_ice = rtp_pair.remote_ip.split(":")
                rtp_transport.remote_rtp_port_ice = int(rtp_transport.remote_rtp_port_ice)
                rtp_transport._local_rtp_candidate_type = rtp_pair.local_type
                rtp_transport._remote_rtp_candidate_type = rtp_pair.remote_type
                _add_event("RTPTransportICENegotiationDidSucceed", dict(obj=rtp_transport, chosen_local_candidates=chosen_local_candidates, chosen_remote_candidates=chosen_remote_candidates, duration=duration, local_candidates=local_cand_list, remote_candidates=remote_cand_list, connectivity_checks_results=v_list))
    except:
        ua._handle_exception(1)

cdef void _RTPTransport_cb_ice_failure(pjmedia_transport *tp, char *reason) with gil:
    cdef void *rtp_transport_void = NULL
    cdef RTPTransport rtp_transport
    cdef PJSIPUA ua
    try:
        ua = _get_ua()
    except:
        return
    try:
        if tp != NULL:
            rtp_transport_void = (<void **> (tp.name + 1))[0]
        if rtp_transport_void != NULL:
            rtp_transport = (<object> rtp_transport_void)()
            if rtp_transport is None:
                return
            _reason = reason
            if _reason != "media stop requested":
                rtp_transport._local_rtp_candidate_type = None
                rtp_transport._remote_rtp_candidate_type = None
                _add_event("RTPTransportICENegotiationDidFail", dict(obj=rtp_transport, reason=_reason))
    except:
        ua._handle_exception(1)

cdef void _RTPTransport_cb_ice_state(pjmedia_transport *tp, char *state) with gil:
    cdef void *rtp_transport_void = NULL
    cdef RTPTransport rtp_transport
    cdef PJSIPUA ua
    try:
        ua = _get_ua()
    except:
        return
    try:
        if tp != NULL:
            rtp_transport_void = (<void **> (tp.name + 1))[0]
        if rtp_transport_void != NULL:
            rtp_transport = (<object> rtp_transport_void)()
            if rtp_transport is None:
                return
            _add_event("RTPTransportICENegotiationStateDidChange", dict(obj=rtp_transport, state=state))
    except:
        ua._handle_exception(1)

cdef void _AudioTransport_cb_dtmf(pjmedia_stream *stream, void *user_data, int digit) with gil:
    cdef AudioTransport audio_stream = (<object> user_data)()
    cdef PJSIPUA ua
    try:
        ua = _get_ua()
    except:
        return
    if audio_stream is None:
        return
    try:
        _add_event("RTPAudioStreamGotDTMF", dict(obj=audio_stream, digit=chr(digit)))
    except:
        ua._handle_exception(1)

# globals

cdef pjmedia_ice_cb _ice_cb
_ice_cb.on_ice_complete = _RTPTransport_cb_ice_complete
_ice_cb.on_ice_candidates_chosen = _RTPTransport_cb_ice_candidates_chosen
_ice_cb.on_ice_failure = _RTPTransport_cb_ice_failure
_ice_cb.on_ice_state = _RTPTransport_cb_ice_state

