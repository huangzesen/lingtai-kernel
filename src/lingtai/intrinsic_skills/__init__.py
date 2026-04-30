"""Kernel-shipped skill bundles that are not tied to a specific tool.

Each subdirectory is copied verbatim into ``.library/intrinsic/capabilities/<name>/``
on every agent boot by ``lingtai.agent.Agent._install_intrinsic_manuals``.

Use this for documentation-only skills (e.g. ``lingtai-kernel-anatomy``)
that ship with the kernel but don't have companion code under ``core/`` or
``capabilities/``.
"""
