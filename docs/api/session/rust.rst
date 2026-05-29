####
Rust
####

RustSession is an optional client session backed by a PyO3/Rust HTTP transport.

``AiohttpSession`` remains the default session used by :class:`aiogram.Bot`.
``RustSession`` is opt-in and must be passed explicitly.

Usage example
=============

.. code-block:: python

    from aiogram import Bot
    from aiogram.client.session.rust import RustSession

    session = RustSession()
    bot = Bot("42:token", session=session)


Compatibility
=============

``RustSession`` preserves the main client session contract:

- request middleware support through :class:`aiogram.client.session.base.BaseSession`
- Bot API response validation and exception mapping through ``BaseSession.check_response()``
- multipart uploads
- request timeouts
- ``close()``, ``make_request()``, ``stream_content()``, and async context manager usage

The initial upload implementation materializes :class:`aiogram.types.InputFile`
contents in memory before passing them to Rust.

``stream_content()`` currently downloads the response in Rust and yields Python
chunks from the returned bytes.

Packaging
=========

The Rust extension is bundled into aiogram wheels. ``AiohttpSession`` remains
the default session; using the Rust transport still requires explicitly passing
``RustSession`` to :class:`aiogram.Bot`.

Installing aiogram from source requires a working Rust toolchain because the
native extension is built as part of the main aiogram package.


Proxy requests
==============

``RustSession`` accepts a single proxy URL:

.. code-block:: python

    session = RustSession(proxy="http://user:password@host:port")

Proxy chains and ``aiohttp.BasicAuth`` proxy tuples are not supported by
``RustSession``.


Reference
=========

.. autoclass:: aiogram.client.session.rust.RustSession
    :members:
