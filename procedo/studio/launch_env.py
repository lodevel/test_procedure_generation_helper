"""The environment variables Procedo Bench sets when it launches Procedo Studio: Studio's side of
the launch contract, named here alone (Bench names them in
``procedo.bench.workflow_editor_launch``)."""

#: The active bundle's ``defaults.json``; unset when Bench had no bundle at launch.
BUNDLE_DEFAULTS_PATH = "PROCEDO_BUNDLE_DEFAULTS_PATH"
#: Bench's app theme name.
APP_THEME = "PROCEDO_APP_THEME"
#: Bench's state indicator style (Badges or Pictos).
APP_INDICATORS = "PROCEDO_APP_INDICATORS"
