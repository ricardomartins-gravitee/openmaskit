# Shared design system

OpenMaskit's existing name, icon, Python package and upstream fork relationship
are retained. The independently maintained
[OpenMaskit Design System](https://github.com/ricardomartins-gravitee/openmaskit-design-system)
is the common source for dark theme colours, spacing, typography roles, radii
and component styles shared with the native macOS Companion.

`style.css` imports checked-in assets from `static/design/`; the Python wheel
and container include those files. Existing CSS variable names are generated
compatibility aliases, so page layouts and upstream styling stay maintainable.
The privacy panel uses the shared button primitives. The muted text colour is
slightly brighter for normal-text contrast against all standard dark panels.

`design-system.lock.json` records an immutable source revision and SHA-256 hashes.
No assets are downloaded at runtime and no installed service is automatically
updated. To verify or deliberately adopt a tested shared release:

```sh
python3 scripts/design_system.py --check
python3 scripts/design_system.py --check-upstream
python3 scripts/design_system.py --update EXACT_40_CHARACTER_COMMIT
```

The updater downloads only the fixed asset list, validates every checksum before
writing, and retains Apache-2.0 attribution beside the CSS. Commit the new lock
and assets together, run unit/startup/browser acceptance, and coordinate the
same design revision in Companion. A future light theme must be adopted by both
applications before exposing theme settings.
