# encoding: utf8

from __future__ import absolute_import, division, print_function, unicode_literals

from base64 import b64decode, b64encode
from binascii import hexlify, unhexlify
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import errno
import fnmatch
from hashlib import sha256
import hmac
from operator import getitem
import os
import pickle
import re

from six import PY3
from six.moves.urllib.parse import quote as urlquote

from pando import Response, json
from pando.utils import to_rfc822, utcnow
from markupsafe import Markup
from postgres.cursors import SimpleCursorBase

from liberapay.exceptions import AccountSuspended, AuthRequired, LoginRequired, InvalidNumber
from liberapay.models.community import Community
from liberapay.utils.i18n import Money
from liberapay.website import website


BEGINNING_OF_EPOCH = to_rfc822(datetime(1970, 1, 1)).encode('ascii')


class NS(object):
    def __init__(self, d):
        self.__dict__.update(d)


def get_participant(state, restrict=True, redirect_stub=True, allow_member=False,
                    block_suspended_user=False, redirect_canon=True):
    """Given a Request, raise Response or return Participant.

    If restrict is True then we'll restrict access to owners and admins.

    """
    request = state['request']
    response = state['response']
    user = state['user']
    slug = request.line.uri.path['username']
    _ = state['_']

    if restrict and user.ANON:
        raise LoginRequired

    if slug.startswith('~'):
        thing = 'id'
        value = slug[1:]
        participant = user if user and str(user.id) == value else None
    else:
        thing = 'lower(username)'
        value = slug.lower()
        participant = user if user and user.username.lower() == value else None

    if participant is None:
        from liberapay.models.participant import Participant  # avoid circular import
        participant = Participant._from_thing(thing, value) if value else None
        if participant is None:
            raise response.error(404)
        elif participant.kind == 'community':
            c_name = Participant.db.one("""
                SELECT name
                  FROM communities
                 WHERE participant = %s
            """, (participant.id,))
            raise response.redirect('/for/%s' % c_name)

    if redirect_canon and request.method in ('GET', 'HEAD'):
        if slug != participant.username:
            canon = '/' + participant.username + request.line.uri[len(slug)+1:]
            raise response.redirect(canon)

    status = participant.status
    if status == 'closed':
        if user.is_admin:
            return participant
        state['closed_account'] = participant
        response.html_template = 'templates/account-closed.html'
        raise response.error(410)
    elif status == 'stub':
        if redirect_stub:
            to = participant.resolve_stub()
            if not to:
                # Account has been taken over
                raise response.error(404)
            raise response.redirect(to)

    if restrict:
        if participant != user:
            if allow_member and participant.kind == 'group' and user.member_of(participant):
                pass
            elif not user.is_admin:
                raise response.error(403, _("You are not authorized to access this page."))

    if block_suspended_user and participant.is_suspended and participant == user:
        raise AccountSuspended()

    return participant


def get_community(state, restrict=False):
    request, response = state['request'], state['response']
    user = state['user']
    name = request.path['name']

    c = Community.from_name(name)
    if request.method in ('GET', 'HEAD'):
        if not c:
            response.redirect('/for/new?name=' + urlquote(name))
        if c.name != name:
            response.redirect('/for/' + c.name + request.line.uri[5+len(name):])
    elif not c:
        raise response.error(404)
    elif user.ANON:
        raise AuthRequired

    if restrict:
        if user.ANON:
            raise LoginRequired
        if user.id != c.creator and not user.is_admin:
            _ = state['_']
            raise response.error(403, _("You are not authorized to access this page."))

    return c


def b64decode_s(s, **kw):
    def error():
        if 'default' in kw:
            return kw['default']
        raise Response(400, "invalid base64 input")

    try:
        s = s.encode('ascii') if hasattr(s, 'encode') else s
    except UnicodeError:
        return error()

    udecode = lambda a: a.decode('utf8')
    if s[:1] == b'.':
        udecode = lambda a: a
        s = s[1:]
    s = s.replace(b'~', b'=')
    try:
        return udecode(b64decode(s, '-_'))
    except Exception:
        try:
            # For retrocompatibility
            return udecode(b64decode(s))
        except Exception:
            pass
        return error()


def b64encode_s(s):
    prefix = b''
    if not isinstance(s, bytes):
        s = s.encode('utf8')
    else:
        # Check whether the string is binary or already utf8
        try:
            s.decode('utf8')
        except UnicodeError:
            prefix = b'.'
    r = prefix + b64encode(s, b'-_').replace(b'=', b'~')
    return r.decode('ascii') if PY3 else r


def update_global_stats(website):
    website.gnusers = website.db.one("""
        SELECT count(*)
          FROM participants
         WHERE status = 'active'
           AND kind IN ('individual', 'organization');
    """)
    transfer_volume = website.db.one("""
        SELECT coalesce(sum(amount), 0)
          FROM current_tips
         WHERE is_funded
    """)
    website.gmonthly_volume = Money(transfer_volume * 52 / 12, 'EUR')


def _execute(this, sql, params=[]):
    print(sql.strip(), params)
    super(SimpleCursorBase, this).execute(sql, params)

def log_cursor(f):
    "Prints sql and params to stdout. Works globaly so watch for threaded use."
    def wrapper(*a, **kw):
        try:
            SimpleCursorBase.execute = _execute
            ret = f(*a, **kw)
        finally:
            del SimpleCursorBase.execute
        return ret
    return wrapper


def excerpt_intro(text, length=175, append='…'):
    if not text:
        return ''
    if len(text) > length:
        return text[:length] + append
    return text


def is_card_expired(exp_year, exp_month):
    today = date.today()
    cur_year, cur_month = today.year, today.month
    return exp_year < cur_year or exp_year == cur_year and exp_month < cur_month


def ensure_str(s):
    if isinstance(s, str):
        return s
    return s.decode('ascii') if isinstance(s, bytes) else s.encode('ascii')


def set_cookie(cookies, key, value, expires=None, httponly=True, path='/'):
    key = ensure_str(key)
    cookies[key] = ensure_str(value)
    cookie = cookies[key]
    if expires:
        if isinstance(expires, timedelta):
            expires += utcnow()
        if isinstance(expires, datetime):
            expires = to_rfc822(expires)
        cookie[str('expires')] = ensure_str(expires)
    if httponly:
        cookie[str('httponly')] = True
    if path:
        cookie[str('path')] = ensure_str(path)
    if website.cookie_domain:
        cookie[str('domain')] = ensure_str(website.cookie_domain)
    if website.canonical_scheme == 'https':
        cookie[str('secure')] = True


def erase_cookie(cookies, key, **kw):
    set_cookie(cookies, key, '', BEGINNING_OF_EPOCH, **kw)


def to_javascript(obj):
    """For when you want to inject an object into a <script> tag.
    """
    return json.dumps(obj).replace('</', '<\\/')


svg_attrs_re = re.compile(r'\s+(?:height|width|x|y|xmlns)=(["\']).*?\1')

def include_svg(svg, height, width, x=None, y=None):
    """For when you want to include an SVG in an HTML page or in another SVG.
    """
    assert svg.startswith('<svg')
    i = svg.find('>')
    assert i != -1
    d = locals()
    attrs = svg_attrs_re.sub('', svg[4:i])
    for a in ('height', 'width', 'x', 'y'):
        v = d[a]
        if v is None:
            continue
        attrs += ' %s="%s"' % (a, v)
    return Markup(svg[:4] + attrs + svg[i:])


def group_by(iterable, key, attr=False, ignored_exceptions=KeyError):
    r = {}
    if callable(key):
        for obj in iterable:
            k = key(obj)
            r.setdefault(k, []).append(obj)
        return r
    f = getattr if attr else getitem
    for obj in iterable:
        try:
            k = f(obj, key)
        except ignored_exceptions:
            continue
        r.setdefault(k, []).append(obj)
    return r


def find_files(directory, pattern):
    for root, dirs, files in os.walk(directory):
        for filename in fnmatch.filter(files, pattern):
            yield os.path.join(root, filename)


def serialize(context):
    for k, v in context.items():
        if str(type(v)) == "<class 'psycopg2.extras.Record'>":
            context[k] = v._asdict()
    return b'\\x' + hexlify(pickle.dumps(context, 2))


def deserialize(context):
    if isinstance(context, memoryview) and context[:2].tobytes() == b'\\x':
        context = unhexlify(context[2:])
    return pickle.loads(context)


def pid_exists(pid):
    """Check whether pid exists in the current process table. UNIX only.

    Source: http://stackoverflow.com/a/6940314/2729778
    """
    if not pid > 0:
        raise ValueError("bad PID %s" % pid)
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


def build_s3_object_url(key):
    now = utcnow()
    timestamp = now.strftime('%Y%m%dT%H%M%SZ')
    today = timestamp.split('T', 1)[0]
    region = website.app_conf.s3_region
    access_key = website.app_conf.s3_public_access_key
    endpoint = website.app_conf.s3_endpoint
    assert endpoint.startswith('https://')
    host = endpoint[8:]
    querystring = (
        "X-Amz-Algorithm=AWS4-HMAC-SHA256&"
        "X-Amz-Credential={access_key}%2F{today}%2F{region}%2Fs3%2Faws4_request&"
        "X-Amz-Date={timestamp}&"
        "X-Amz-Expires=86400&"
        "X-Amz-SignedHeaders=host"
    ).format(**locals())
    canonical_request = (
        "GET\n"
        "/{key}\n"
        "{querystring}\n"
        "host:{host}\n"
        "\n"
        "host\n"
        "UNSIGNED-PAYLOAD"
    ).format(**locals()).encode()
    canonical_request_hash = sha256(canonical_request).hexdigest()
    string_to_sign = (
        "AWS4-HMAC-SHA256\n"
        "{timestamp}\n"
        "{today}/{region}/s3/aws4_request\n"
        "{canonical_request_hash}"
    ).format(**locals()).encode()
    aws4_secret_key = b"AWS4" + website.app_conf.s3_secret_key.encode()
    sig_key = hmac.new(aws4_secret_key, today.encode(), sha256).digest()
    sig_key = hmac.new(sig_key, region.encode(), sha256).digest()
    sig_key = hmac.new(sig_key, b"s3", sha256).digest()
    sig_key = hmac.new(sig_key, b"aws4_request", sha256).digest()
    signature = hmac.new(sig_key, string_to_sign, sha256).hexdigest()
    return endpoint + "/" + key + "?" + querystring + "&X-Amz-Signature=" + signature


def read_decimal_or_400(s):
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        raise InvalidNumber(s)
