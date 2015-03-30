# -*- coding: utf-8 -*-
"""Apple Push Notification Service

Documentation is available on the iOS Developer Library: http://goo.gl/wFVr2S
"""

from binascii import unhexlify
from contextlib import closing
import socket
import ssl
import struct
import time

from .utils import json_dumps
from .exceptions import (
    APNSError,
    APNSAuthError,
    APNSInvalidTokenError,
    APNSInvalidPayloadSizeError,
    raise_apns_server_error
)


__all__ = (
    'send',
    'send_bulk',
    'get_expired_tokens',
)


# Apple protocol says command is always 8. See http://goo.gl/ENUjXg
APNS_ERROR_RESPONSE_COMMAND = 8


def is_valid_token(token):
    """Check if token is valid format."""
    try:
        assert unhexlify(token)
        assert len(token) == 64
        valid = True
    except Exception:
        valid = False

    return valid


def create_payload(alert,
                   badge=None,
                   sound=None,
                   category=None,
                   content_available=None,
                   title=None,
                   title_loc_key=None,
                   title_loc_args=None,
                   action_loc_key=None,
                   loc_key=None,
                   loc_args=None,
                   launch_image=None,
                   extra=None,
                   **ignore):
    """Return notification payload in JSON format."""
    if loc_args is None:
        loc_args = []

    if extra is None:
        extra = {}

    payload = {}
    payload.update(extra)
    payload['aps'] = {}

    if any([title,
            title_loc_key,
            title_loc_args,
            action_loc_key,
            loc_key,
            loc_args,
            launch_image]):
        alert = {'body': alert} if alert else {}

        if title:
            alert['title'] = title

        if title_loc_key:
            alert['title-loc-key'] = title_loc_key

        if title_loc_args:
            alert['title-loc-args'] = title_loc_args

        if action_loc_key:
            alert['action-loc-key'] = action_loc_key

        if loc_key:
            alert['loc-key'] = loc_key

        if loc_args:
            alert['loc-args'] = loc_args

        if launch_image:
            alert['launch-image'] = launch_image

    if alert:
        payload['aps']['alert'] = alert

    if badge:
        payload['aps']['badge'] = badge

    if sound:
        payload['aps']['sound'] = sound

    if category:
        payload['aps']['category'] = category

    if content_available:
        payload['aps']['content-available'] = 1

    return json_dumps(payload)


def create_socket(host, port, certfile):
    """Create a socket connection to the APNS server."""
    if not certfile:
        raise APNSAuthError(('Missing certificate file. '
                             'Cannot send notifications.'))

    try:
        with open(certfile, 'r') as f:
            f.read()
    except Exception as ex:
        raise APNSAuthError(('The certfile at {0} is not readable: {1}'
                             .format(certfile, ex)))

    connection = socket.socket()

    # For some reason, pylint on TravisCI's Python 2.7 platform complains that
    # ssl.PROTOCOL_TLSv1 doesn't exist. Add a disable flag to bypass this.
    # pylint: disable=no-member
    connection = ssl.wrap_socket(connection,
                                 ssl_version=ssl.PROTOCOL_TLSv1,
                                 certfile=certfile)
    connection.connect((host, port))

    return connection


def create_push_socket(config):
    """Return socket connection to push server."""
    return create_socket(config['APNS_HOST'],
                         config['APNS_PORT'],
                         config['APNS_CERTIFICATE'])


def create_feedback_socket(config):
    """Return socket connection to feedback server."""
    return create_socket(config['APNS_FEEDBACK_HOST'],
                         config['APNS_FEEDBACK_PORT'],
                         config['APNS_CERTIFICATE'])


def check_errors(connection, config):
    """Check socket response for errors and raise status based exception if
    found.
    """
    timeout = config['APNS_ERROR_TIMEOUT']

    if timeout is None:
        # Assume everything went fine.
        return

    original_timeout = connection.gettimeout()

    try:
        connection.settimeout(timeout)
        data = connection.recv(6)

        if data:
            command, status, identifier = struct.unpack("!BBI", data)

            if command != APNS_ERROR_RESPONSE_COMMAND:
                raise APNSError(('Error response command must be {0}. '
                                 'Found: {1}'
                                 .format(APNS_ERROR_RESPONSE_COMMAND,
                                         command)))

            if status != 0:
                raise_apns_server_error(status, identifier)
    except socket.timeout:  # pragma: no cover
        # py3, See http://bugs.python.org/issue10272
        pass
    except ssl.SSLError as ex:  # pragma: no cover
        # py2
        if 'timed out' not in ex.message:
            raise
    finally:
        connection.settimeout(original_timeout)


def pack_frame(token, payload, identifier, expiration, priority):
    """Return packed socket frame."""
    token_bin = unhexlify(token)
    token_len = len(token_bin)
    payload_len = len(payload)

    # |COMMAND|FRAME-LEN|{token}|{payload}|{id:4}|{expiration:4}|{priority:1}
    # 5 items, each 3 bytes prefix, then each item length
    frame_len = 3 * 5 + token_len + payload_len + 4 + 4 + 1
    frame_fmt = '!BIBH{0}sBH{1}sBHIBHIBHB'.format(token_len, payload_len)
    frame = struct.pack(frame_fmt,
                        2, frame_len,
                        1, token_len, token_bin,
                        2, payload_len, payload,
                        3, 4, identifier,
                        4, 4, expiration,
                        5, 1, priority)

    return frame


def read_and_unpack(connection, data_format):
    """Unpack and return socket frame."""
    length = struct.calcsize(data_format)
    data = connection.recv(length)

    if data:
        return struct.unpack_from(data_format, data, 0)
    else:
        return None


def receive_feedback(connection):
    """Return expired tokens from feedback server."""
    expired_tokens = []

    # Read a timestamp (4 bytes) and device token length (2 bytes).
    header_format = '!LH'
    has_data = True

    while has_data:
        try:
            # Read the header tuple.
            header_data = read_and_unpack(connection, header_format)

            if header_data is not None:
                timestamp, token_length = header_data

                # Unpack format for a single value of length bytes
                device_token = read_and_unpack(connection,
                                               '{0}s'.format(token_length))

                if device_token is not None:
                    token = device_token[0].encode('hex')
                    expired_tokens.append((token, timestamp))
            else:
                has_data = False
        except socket.timeout:  # pragma: no cover
            # py3, see http://bugs.python.org/issue10272
            pass
        except ssl.SSLError as ex:  # pragma: no cover
            # py2
            if 'timed out' not in ex.message:
                raise

    return expired_tokens


def send(token,
         data,
         config,
         identifier=0,
         expiration=None,
         priority=10,
         payload=None,
         connection=None,
         **options):
    """Send push notification to single device.

    Args:
        token (str): APNS device token. Expected to be a 64 character hex
            string.
        data (str|dict): Alert message or dictionary.
        config (dict): Configuration dictionary containing APNS configuration
            values. See :mod:`pushjack.config` for more details.
        identifier (int, optional): Message identifier. Defaults to ``0``.
        expiration (int, optional): Expiration time of message in seconds
            offset from now. Defaults to ``None`` which uses
            ``config['APNS_DEFAULT_EXPIRATION_OFFSET']``.
        priority (int, optional): The alert’s priority. Provide one of the
            following values:

            - 10
                The push message is sent immediately. The remote notification
                must trigger an alert, sound, or badge on the device. It is an
                error to use this priority for a push that contains only the
                ``content_available`` key.
            - 5
                The push message is sent at a time that conserves power on the
                device receiving it.

            Defaults to ``10``.
        payload (str, optional): Directly send alert payload as JSON formatted
            string. If set then alert arguments are ignored and `payload` is
            used directly. Defaults to ``None`` which results in `payload`
            being constructed from passed in arguments.
        connection (socket, optional): Provide outside socket connection to
            APNS server. Socket is assumed to have been preconfigured and ready
            to use. When `connection` is provided, no error checking is done;
            it's assumed that the connection provider will handle that
            themselves.

    Keyword Args:
        badge (int, optional): Badge number count for alert. Defaults to
            ``None``.
        sound (str, optional): Name of the sound file to play for alert.
            Defaults to ``None``.
        category (str, optional): Name of category. Defaults to ``None``.
        content_available (bool, optional): If ``True``, indicate that new
            content is available. Defaults to ``None``.
        title (str, optional): Alert title.
        title_loc_key (str, optional): The key to a title string in the
            ``Localizable.strings`` file for the current localization.
        title_loc_args (list, optional): List of string values to appear in
            place of the format specifiers in `title_loc_key`.
        action_loc_key (str, optional): Display an alert that includes the
            ``Close`` and ``View`` buttons. The string is used as a key to get
            a localized string in the current localization to use for the right
            button’s title instead of ``“View”``.
        loc_key (str, optional): A key to an alert-message string in a
            ``Localizable.strings`` file for the current localization.
        loc_args (list, optional): List of string values to appear in place of
            the format specifiers in ``loc_key``.
        launch_image (str, optional): The filename of an image file in the app
            bundle; it may include the extension or omit it.
        extra (dict, optional): Extra data to include with the alert.

    Returns:
        None

    Raises:
        APNSInvalidTokenError: Invalid token format.
        APNSInvalidPayloadSizeError: Notification payload size too large.
        APNSServerError: APNS error response from server.

    .. versionadded:: 0.0.1
    """
    if not is_valid_token(token):
        raise APNSInvalidTokenError(('Invalid token format. '
                                     'Expected 64 character hex string.'))

    if payload is None:
        payload = create_payload(data, **options)

    max_size = config['APNS_MAX_NOTIFICATION_SIZE']
    default_expiration_offset = config['APNS_DEFAULT_EXPIRATION_OFFSET']

    if len(payload) > max_size:
        raise APNSInvalidPayloadSizeError(('Notification body cannot exceed '
                                           '{0} bytes'
                                           .format(max_size)))

    # If expiration isn't specified use default offset from now.
    expiration_time = (expiration if expiration is not None
                       else int(time.time()) + default_expiration_offset)

    frame = pack_frame(token,
                       payload,
                       identifier,
                       expiration_time,
                       priority)

    if connection:
        connection.write(frame)
    else:
        with closing(create_push_socket(config)) as _connection:
            _connection.write(frame)
            check_errors(_connection, config)


def send_bulk(tokens, data, config, payload=None, **options):
    """Send push notification to multiple devices.

    Args:
        tokens (list): List of APNS device tokens. Each token is expected to be
            a 64 character hex string.
        data (str|dict): Alert message or dictionary.
        config (dict): Configuration dictionary containing APNS configuration
            values. See :mod:`pushjack.config` for more details.
        payload (str, optional): Directly send alert payload as JSON formatted
            string. If set then alert arguments are ignored and `payload` is
            used directly. Defaults to ``None`` which results in `payload`
            being constructed from passed in arguments.

    Returns:
        None

    See Also:
        See :func:`send` for a full listing of keyword arguments.

    .. versionadded:: 0.0.1
    """
    if payload is None:
        # Reuse payload since it's identical for each send.
        payload = create_payload(data, **options)

    with closing(create_push_socket(config)) as connection:
        for identifier, token in enumerate(tokens):
            send(token,
                 data,
                 config,
                 identifier=identifier,
                 payload=payload,
                 connection=connection,
                 **options)

        check_errors(connection, config)


def get_expired_tokens(config):
    """Return inactive device ids that can't be pushed to anymore.

    Args:
        config (dict): Configuration dictionary containing APNS configuration
            values. See :mod:`pushjack.config` for more details.

    Returns:
        list: List of tuples containing ``(token, timestamp)``.

    .. versionadded:: 0.0.1
    """
    with closing(create_feedback_socket(config)) as connection:
        return receive_feedback(connection)
