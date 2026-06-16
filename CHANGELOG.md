# Changelog

All notable changes to this project are documented here. The format is
loosely [Keep a Changelog](https://keepachangelog.com/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Initial implementation: client + server, X11 and Wayland backends,
  H.264/JPEG-delta encoders, multiplexed SSH transport, SFTP file
  transfer with per-user jail, two-way text clipboard, in-app SSH key
  generation, file manager dialog, drag-and-drop upload.
- Pytest suite (protocol, messages, framing, keygen, file jail,
  encoder/decoder, config, broker loopback).
- GitHub Actions CI (lint + tests on Linux + Windows).

## [1.0.0] - 2026-06-16

- First public release.
