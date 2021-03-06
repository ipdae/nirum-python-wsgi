""":mod:`nirum_wsgi` --- Nirum services as WSGI apps
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

"""
import argparse
import collections
import json
import re
import typing

from nirum.datastructures import List
from nirum.deserialize import deserialize_meta
from nirum.exc import (NirumProcedureArgumentRequiredError,
                       NirumProcedureArgumentValueError)
from nirum.serialize import serialize_meta
from nirum.service import Service
from six import integer_types, text_type
from six.moves import reduce
from six.moves.urllib import parse as urlparse
from werkzeug.http import HTTP_STATUS_CODES
from werkzeug.serving import run_simple
from werkzeug.wrappers import Request, Response

__version__ = '0.2.0'
__all__ = ('AnnotationError', 'InvalidJsonError', 'ServiceMethodError',
           'WsgiApp')


def parse_json_payload(request):
    payload = request.get_data(as_text=True)
    if payload:
        try:
            json_payload = json.loads(payload)
        except (TypeError, ValueError):
            raise InvalidJsonError(payload)
        else:
            return json_payload
    else:
        return {}


class InvalidJsonError(ValueError):
    """Exception raised when a payload is not a valid JSON."""


class AnnotationError(ValueError):
    """Exception raised when the given Nirum annotation is invalid."""


class ServiceMethodError(LookupError):
    """Exception raised when a method is not found."""


class WsgiApp:
    """Create a WSGI application which adapts the given Nirum service.

    :param service: A service instance (not type) generated by Nirum compiler.
    :type service: :class:`nirum.service.Service`
    :param allowed_origins: A set of cross-domain origins allowed to access.
                            See also CORS_.
    :type allowed_origins: :class:`~typing.AbstractSet`\ [:class:`str`]
    :param allowed_headers: A set of allowed headers to request headers.
                            See also CORS_.
    :type allowed_headers: :class:`~typing.AbstractSet`\ [:class:`str`]

    .. _CORS: https://www.w3.org/TR/cors/

    """

    def __init__(self, service,
                 allowed_origins=frozenset(),
                 allowed_headers=frozenset()):
        if not isinstance(service, Service):
            if isinstance(service, type) and issubclass(service, Service):
                raise TypeError('expected an instance of {0.__module__}.'
                                '{0.__name__}, not uninstantiated service '
                                'class itself'.format(Service))
            raise TypeError(
                'expected an instance of {0.__module__}.{0.__name__}, not '
                '{1!r}'.format(Service, service)
            )
        elif not isinstance(allowed_origins, collections.Set):
            raise TypeError('allowed_origins must be a set, not ' +
                            repr(allowed_origins))
        self.service = service
        self.allowed_origins = frozenset(d.strip().lower()
                                         for d in allowed_origins)
        self.allowed_headers = frozenset(h.strip().lower()
                                         for h in allowed_headers)
        rules = []
        method_annoations = service.__nirum_method_annotations__
        service_methods = service.__nirum_service_methods__
        for method_name, annotations in method_annoations.items():
            try:
                params = annotations['http_resource']
            except KeyError:
                continue
            if not params['path'].lstrip('/'):
                raise AnnotationError(
                    'the root resource is reserved; '
                    'disallowed to route to the root'
                )
            try:
                uri_template = params['path']
                path_pattern, variables = compile_uri_template(uri_template)
                http_verb = params['method']
            except KeyError as e:
                raise AnnotationError('missing annotation parameter: ' +
                                      str(e))
            parameters = frozenset(
                service_methods[method_name]['_names'].values()
            )
            unsatisfied_parameters = parameters - variables
            if unsatisfied_parameters:
                raise AnnotationError(
                    '"{0}" does not fully satisfy all parameters of {1}() '
                    'method; unsatisfied parameters are: {2}'.format(
                        uri_template, method_name,
                        ', '.join(sorted(unsatisfied_parameters))
                    )
                )
            rules.append((
                uri_template,
                path_pattern,
                http_verb,
                method_name  # Service method
            ))
        rules.sort(key=lambda rule: rule[0], reverse=True)
        self.rules = List(rules)

    def __call__(self, environ, start_response):
        """

        :param environ:
        :param start_response:

        """
        return self.route(environ, start_response)

    def route(self, environ, start_response):
        """Route

        :param environ:
        :param start_response:

        """
        request = Request(environ)
        service_methods = self.service.__nirum_service_methods__
        error_raised = None
        for _, pattern, verb, method_name in self.rules:
            path_info = environ['PATH_INFO']
            if isinstance(path_info, bytes):
                # FIXME Decode properly; URI is not unicode
                path_info = path_info.decode()
            match = pattern.match(path_info)
            if match and environ['REQUEST_METHOD'] == verb.upper():
                routed = True
                service_method = method_name
                if verb in ('GET', 'DELETE'):
                    method_parameters = {
                        k: v
                        for k, v in service_methods[method_name].items()
                        if not k.startswith('_')
                    }
                    # TODO Parsing query string
                    payload = {p: match.group(p) for p in method_parameters}
                else:
                    try:
                        payload = parse_json_payload(request)
                    except InvalidJsonError as e:
                        error_raised = self.error(
                            400, request,
                            message="Invalid JSON payload: '{!s}'.".format(e)
                        )
                cors_headers = []  # TODO
                break
        else:
            routed = False
            if request.method not in ('POST', 'OPTIONS'):
                error_raised = self.error(405, request)

            # CORS
            cors_headers = [
                ('Access-Control-Allow-Methods', 'POST, OPTIONS'),
                ('Vary', 'Origin'),
            ]
            if self.allowed_headers:
                cors_headers.append(
                    (
                        'Access-Control-Allow-Headers',
                        ', '.join(sorted(self.allowed_headers))
                    )
                )
            try:
                origin = request.headers['Origin']
            except KeyError:
                pass
            else:
                parsed_origin = urlparse.urlparse(origin)
                if parsed_origin.scheme in ('http', 'https') and \
                   parsed_origin.hostname in self.allowed_origins:
                    cors_headers.append(
                        ('Access-Control-Allow-Origin', origin)
                    )

            if request.method == 'OPTIONS':
                start_response('200 OK', cors_headers)
                return []
            service_method = request.args.get('method')
            try:
                payload = parse_json_payload(request)
            except InvalidJsonError as e:
                error_raised = self.error(
                    400, request,
                    message="Invalid JSON payload: '{!s}'.".format(e)
                )
        if error_raised:
            response = error_raised
        elif service_method:
            try:
                response = self.rpc(request, service_method, payload)
            except ServiceMethodError:
                response = self.error(
                    404 if routed else 400, request,
                    message='No service method `{}` found.'.format(
                        service_method
                    )
                )
            else:
                for k, v in cors_headers:
                    if k in response.headers:
                        response.headers[k] += ', ' + v  # FIXME: is it proper?
                    else:
                        response.headers[k] = v
        else:
            response = self.error(
                400, request,
                message="`method` is missing."
            )
        return response(environ, start_response)

    def rpc(self, request, service_method, request_json):
        name_map = self.service.__nirum_method_names__
        try:
            method_facial_name = name_map.behind_names[service_method]
        except KeyError:
            raise ServiceMethodError()
        try:
            func = getattr(self.service, method_facial_name)
        except AttributeError:
            return self.error(
                400,
                request,
                message="Service has no procedure '{}'.".format(service_method)
            )
        if not callable(func):
            return self.error(
                400, request,
                message="Remote procedure '{}' is not callable.".format(
                    service_method
                )
            )
        type_hints = self.service.__nirum_service_methods__[method_facial_name]
        try:
            arguments = self._parse_procedure_arguments(
                type_hints,
                request_json
            )
        except (NirumProcedureArgumentValueError,
                NirumProcedureArgumentRequiredError) as e:
            return self.error(400, request, message=str(e))
        method_error_types = self.service.__nirum_method_error_types__
        if not callable(method_error_types):  # generated by older compiler
            method_error_types = method_error_types.get
        method_error = method_error_types(method_facial_name, ())
        try:
            result = func(**arguments)
        except method_error as e:
            return self._raw_response(400, serialize_meta(e))
        return_type = type_hints['_return']
        if type_hints.get('_v', 1) >= 2:
            return_type = return_type()
        if not self._check_return_type(return_type, result):
            return self.error(
                400,
                request,
                message="Incorrect return type '{0}' "
                        "for '{1}'. expected '{2}'.".format(
                            typing._type_repr(result.__class__),
                            service_method,
                            typing._type_repr(return_type)
                        )
            )
        else:
            return self._raw_response(200, serialize_meta(result))

    def _parse_procedure_arguments(self, type_hints, request_json):
        arguments = {}
        version = type_hints.get('_v', 1)
        name_map = type_hints['_names']
        for argument_name, type_ in type_hints.items():
            if argument_name.startswith('_'):
                continue
            if version >= 2:
                type_ = type_()
            behind_name = name_map[argument_name]
            try:
                data = request_json[behind_name]
            except KeyError:
                raise NirumProcedureArgumentRequiredError(
                    "A argument named '{}' is missing, it is required.".format(
                        behind_name
                    )
                )
            try:
                arguments[argument_name] = deserialize_meta(type_, data)
            except ValueError:
                raise NirumProcedureArgumentValueError(
                    "Incorrect type '{0}' for '{1}'. "
                    "expected '{2}'.".format(
                        typing._type_repr(data.__class__), behind_name,
                        typing._type_repr(type_)
                    )
                )
        return arguments

    def _check_return_type(self, type_hint, procedure_result):
        try:
            deserialize_meta(type_hint, serialize_meta(procedure_result))
        except ValueError:
            return False
        else:
            return True

    def make_error_response(self, error_type, message=None):
        """Create error response json temporary.

        .. code-block:: nirum

           union error
               = not-found (text message)
               | bad-request (text message)
               | ...

        """
        # FIXME error response has to be generated from nirum core.
        return {
            '_type': 'error',
            '_tag': error_type,
            'message': message,
        }

    def error(self, status_code, request, message=None):
        """Handle error response.

        :param int status_code:
        :param request:
        :return:

        """
        status_code_text = HTTP_STATUS_CODES.get(status_code, 'http error')
        status_error_tag = status_code_text.lower().replace(' ', '_')
        custom_response_map = {
            404: self.make_error_response(
                status_error_tag,
                'The requested URL {} was not found on this service.'.format(
                    request.path
                )
            ),
            400: self.make_error_response(status_error_tag, message),
            405: self.make_error_response(
                status_error_tag,
                'The requested URL {} was not allowed HTTP method {}.'.format(
                    request.path, request.method
                )
            ),
        }
        return self._raw_response(
            status_code,
            custom_response_map.get(
                status_code,
                self.make_error_response(
                    status_error_tag, message or status_code_text
                )
            )
        )

    def make_response(self, status_code, headers, content):
        return status_code, headers, content

    def _raw_response(self, status_code, response_json, **kwargs):
        response_tuple = self.make_response(
            status_code, headers=[('Content-type', 'application/json')],
            content=json.dumps(response_json).encode('utf-8')
        )
        if not (isinstance(response_tuple, collections.Sequence) and
                len(response_tuple) == 3):
            raise TypeError(
                'make_response() must return a triple of '
                '(status_code, headers, content), not ' + repr(response_tuple)
            )
        status_code, headers, content = response_tuple
        if not isinstance(status_code, integer_types):
            raise TypeError(
                '`status_code` have to be instance of integer. not {}'.format(
                    typing._type_repr(type(status_code))
                )
            )
        if not isinstance(headers, collections.Sequence):
            raise TypeError(
                '`headers` have to be instance of sequence. not {}'.format(
                    typing._type_repr(type(headers))
                )
            )
        if not isinstance(content, bytes):
            raise TypeError(
                '`content` have to be instance of bytes. not {}'.format(
                    typing._type_repr(type(content))
                )
            )
        return Response(content, status_code, headers, **kwargs)


def compile_uri_template(template):
    if not isinstance(template, text_type):
        raise TypeError('template must be a Unicode string, not ' +
                        repr(template))
    value_pattern = re.compile('\{([a-zA-Z0-9_-]+)\}')
    result = []
    variables = set()
    last_pos = 0
    for match in value_pattern.finditer(template):
        variable = match.group(1).replace(u'-', u'_')
        result.append(re.escape(template[last_pos:match.start()]))
        result.append(u'(?P<')
        result.append(variable)
        result.append(u'>.+?)')
        last_pos = match.end()
        if variable in variables:
            raise AnnotationError('every variable must not be duplicated: ' +
                                  variable)
        variables.add(variable)
    result.append(re.escape(template[last_pos:]))
    result.append(u'$')
    return re.compile(u''.join(result)), variables


IMPORT_RE = re.compile(
    r'''^
        (?P<modname> (?!\d) [\w]+
                     (?: \. (?!\d)[\w]+ )*
        )
        :
        (?P<clsexp> (?P<clsname> (?!\d) \w+ )
                    (?: \(.*\) )?
        )
    $''',
    re.X
)


def import_string(imp):
    m = IMPORT_RE.match(imp)
    if not m:
        raise ValueError(
            "malformed expression: {}, have to be x.y:z(...)".format(imp)
        )
    module_name = m.group('modname')
    import_root_mod = __import__(module_name)
    # it is used in `eval()`
    import_mod = reduce(getattr, module_name.split('.')[1:], import_root_mod)
    class_expression = m.group('clsexp')
    try:
        v = eval(class_expression, import_mod.__dict__, {})
    except AttributeError:
        raise ValueError("Can't import {}".format(imp))
    else:
        return v


def main():
    parser = argparse.ArgumentParser(description='Nirum service runner')
    parser.add_argument('-H', '--host', help='the host to listen',
                        default='0.0.0.0')
    parser.add_argument('-p', '--port', help='the port number to listen',
                        type=int, default=9322)
    parser.add_argument('-d', '--debug', help='debug mode',
                        action='store_true', default=False)
    parser.add_argument('service', help='Import path to service instance')
    args = parser.parse_args()
    service = import_string(args.service)
    run_simple(
        args.host, args.port, WsgiApp(service),
        use_reloader=args.debug, use_debugger=args.debug,
        use_evalex=args.debug
    )
