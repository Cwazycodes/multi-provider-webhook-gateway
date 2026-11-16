# Webhook Gateway

A secure, reliable webhook gateway built with Python.

**Status:** Work in progress.

## Overview

This project aims to provide a secure gateway for receiving,
validating, processing and forwarding webhooks.

## Development

You need Python 3.14 (via pyenv), Poetry 2 and GNU Make 4 or newer. macOS
ships Make 3.81, which the Makefile refuses: run `brew install make` and put
`$(brew --prefix)/opt/make/libexec/gnubin` first on your `PATH`.

```bash
make install   # once, after cloning
make check     # lint, typecheck and test
make help      # every other command
```

Every command in this repo is a make target, so `make help` is the full list.

