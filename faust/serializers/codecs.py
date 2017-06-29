"""Serialization utilities.

Supported codecs
================

* **json**    - json with utf-8 encoding.
* **pickle**  - pickle with base64 encoding (not urlsafe)
* **binary**  - base64 encoding (not urlsafe)

Serialization by name
=====================

The func:`dumps` function takes a codec name and the object to encode,
the return value is bytes:

.. code-block:: pycon

    >>> s = dumps('json', obj)

For the reverse direction, the func:`loads` function takes a codec
name and a encoded payload to decode (bytes):

.. code-block:: pycon

    >>> obj = loads('json', s)

You can also combine encoders in the name, like in this case
where json is combined with gzip compression:

.. code-block:: pycon

    >>> obj = loads('json|gzip', s)

Codec registry
==============

Codecs are configured by name and this module maintains
a mapping from name to :class:`Codec` instance: the :attr:`codecs`
attribute.

You can add a new codec to this mapping by:

.. code-block:: pycon

    >>> from faust.serializers import codecs
    >>> codecs.register(custom, custom_serializer())

A codec subclass requires two methods to be implemented: ``_loads()``
and ``_dumps()``:

.. code-block:: python

    import msgpack

    from faust.serializers import codecs

    class raw_msgpack(codecs.Codec):

        def _dumps(self, obj: Any) -> bytes:
            return msgpack.dumps(obj)

        def _loads(self, s: bytes) -> Any:
            return msgpack.loads(s)

Our codec now encodes/decodes to raw msgpack format, but we
may also need to transfer this payload on a transport not
handling binary data well.  Codecs may be chained together,
so to add a text encoding like base64, which we use in this case,
we use the ``|`` operator to form a combined codec:

.. code-block:: python

    def msgpack() -> codecs.Codec:
        return raw_msgpack() | codecs.binary()

    codecs.register('msgpack', msgpack())

At this point we monkey-patched Faust to support
our codec, and we can use it to define records:

.. code-block:: pycon

    >>> from faust.serializers import Record
    >>> class Point(Record, serializer='msgpack'):
    ...     x: int
    ...     y: int

The problem with monkey-patching is that we must make sure the patching
happens before we use the feature.

Faust also supports registering *codec extensions*
using setuptools entrypoints, so instead we can create an installable msgpack
extension.

To do so we need to define a package with the following directory layout:

.. code-block:: text

    faust-msgpack/
        setup.py
        faust_msgpack.py

The first file, :file:`faust-msgpack/setup.py`, defines metadata about our
package and should look like the following example:

.. code-block:: python

    import setuptools

    setuptools.setup(
        name='faust-msgpack',
        version='1.0.0',
        description='Faust msgpack serialization support',
        author='Ola A. Normann',
        author_email='ola@normann.no',
        url='http://github.com/example/faust-msgpack',
        platforms=['any'],
        license='BSD',
        packages=find_packages(exclude=['ez_setup', 'tests', 'tests.*']),
        zip_safe=False,
        install_requires=['msgpack-python'],
        tests_require=[],
        entry_points={
            'faust.codecs': [
                'msgpack = faust_msgpack:msgpack',
            ],
        },
    )

The most important part being the ``entry_points`` key which tells
Faust how to load our plugin. We have set the name of our
codec to ``msgpack`` and the path to the codec class
to be ``faust_msgpack:msgpack``. This will be imported by Faust
as ``from faust_msgpack import msgpack``, so we need to define
that part next in our :file:`faust-msgpack/faust_msgpack.py` module:

.. code-block:: python

    from faust.serializers import codecs

    class raw_msgpack(codecs.Codec):

        def _dumps(self, obj: Any) -> bytes:
            return msgpack.dumps(s)


    def msgpack() -> codecs.Codec:
        return raw_msgpack() | codecs.binary()

That's it! To install and use our new extension we do:

.. code-block:: console

    $ python setup.py install

At this point may want to publish this on PyPI to share
the extension with other Faust users.
"""
import pickle as _pickle
from base64 import b64decode, b64encode
from functools import reduce
from typing import Any, Dict, MutableMapping, Optional, Tuple, cast
from ..types.codecs import CodecArg, CodecT
from ..utils import json as _json
from ..utils.compat import want_bytes, want_str
from ..utils.imports import load_extension_classes

__all__ = [
    'Codec', 'CodecArg', 'register', 'get_codec', 'dumps', 'loads',
]


class Codec(CodecT):
    """Base class for codecs."""

    #: children contains the codecs below us.
    children: Tuple[CodecT, ...]

    #: cached version of children including this codec as the first node.
    # could use chain below, but seems premature so just copying the list.
    nodes: Tuple[CodecT, ...]

    #: subclasses can support keyword arguments,
    #: the base implementation of :meth:`clone` uses this to
    #: preserve keyword arguments in copies.
    kwargs: Dict

    def __init__(self,
                 children: Tuple[CodecT, ...] = None,
                 **kwargs: Any) -> None:
        self.children = children or ()
        self.nodes = (self,) + self.children  # type: ignore
        self.kwargs = kwargs

    def _loads(self, s: bytes) -> Any:
        # subclasses must implement this method.
        raise NotImplementedError()

    def _dumps(self, s: Any) -> bytes:
        # subclasses must implement this method.
        raise NotImplementedError()

    def dumps(self, obj: Any) -> bytes:
        """Encode object ``obj``."""
        # send _dumps to this instance, and all children.
        return reduce(
            lambda obj, e: cast(Codec, e)._dumps(obj),
            self.nodes, obj)

    def loads(self, s: bytes) -> Any:
        """Decode object from string."""
        # send _loads to this instance, and all children in reverse order
        return reduce(
            lambda s, d: cast(Codec, d)._loads(s),
            reversed(self.nodes), s)

    def clone(self, *children: CodecT) -> CodecT:
        """Create a clone of this codec, with optional children added."""
        new_children = self.children + children
        return type(self)(children=new_children, **self.kwargs)

    def __or__(self, other: Any) -> Any:
        # codecs can be chained together, e.g. binary() | json()
        if isinstance(other, CodecT):
            return self.clone(other)
        return NotImplemented

    def __repr__(self) -> str:
        return ' | '.join(
            '{0}({1})'.format(
                type(n).__name__,
                ', '.join(map(repr, cast(Codec, n).kwargs.values())))
            for n in self.nodes
        )


class json(Codec):
    """:mod:`json` serializer."""

    def _loads(self, s: bytes) -> Any:
        return _json.loads(want_str(s))

    def _dumps(self, s: Any) -> bytes:
        return want_bytes(_json.dumps(s))


class raw_pickle(Codec):
    """:mod:`pickle` serializer with no encoding."""

    def _loads(self, s: bytes) -> Any:
        return _pickle.loads(s)

    def _dumps(self, obj: Any) -> bytes:
        return _pickle.dumps(obj)


def pickle() -> Codec:
    """:mod:`pickle` serializer with base64 encoding."""
    return raw_pickle() | binary()


class binary(Codec):
    """Codec for binary content (uses Base64 encoding)."""

    def _loads(self, s: bytes) -> Any:
        return b64decode(s)

    def _dumps(self, s: bytes) -> bytes:
        return b64encode(want_bytes(s))


#: Codec registry, mapping of name to :class:`Codec` instance.
codecs: MutableMapping[str, CodecT] = {
    'json': json(),
    'pickle': pickle(),
    'binary': binary(),
}

#: Cached extension classes.
#: We have to defer extension loading to runtime as the
#: extensions will import from this module causing a circular import.
_extensions_finalized: MutableMapping[str, bool] = {}


def register(name: str, codec: CodecT) -> None:
    codecs[name] = codec


def _maybe_load_extension_classes(
        namespace: str = 'faust.codecs') -> None:
    if namespace not in _extensions_finalized:
        codecs.update({
            name: cls()
            for name, cls in load_extension_classes(namespace)
        })


def _reduce_node(a: Any, b: Any) -> Any:
    return cast(CodecT, codecs.get(a, a)) | codecs[b]


def get_codec(name_or_codec: CodecArg) -> CodecT:
    """Get codec by name."""
    _maybe_load_extension_classes()
    if isinstance(name_or_codec, str):
        if '|' in name_or_codec:
            nodes = name_or_codec.split('|')
            # simple reduce operation, OR (|) them all together:
            return cast(Codec, reduce(_reduce_node, nodes))
        return codecs[name_or_codec]
    return cast(Codec, name_or_codec)


def dumps(codec: Optional[CodecArg], obj: Any) -> bytes:
    """Encode object into bytes."""
    return get_codec(codec).dumps(obj) if codec else obj


def loads(codec: Optional[CodecArg], s: bytes) -> Any:
    """Decode object from bytes."""
    return get_codec(codec).loads(s) if codec else s
