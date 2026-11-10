"""Native Hermes plugin. Import and registration never schedule or infer."""
def register(ctx):
    from .continuity.host import register as native_register
    native_register(ctx)
