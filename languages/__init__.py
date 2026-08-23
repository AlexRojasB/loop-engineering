from languages.dotnet import DotNetAdapter


ADAPTERS = [
    DotNetAdapter(),
]


def detect_adapter(files):
    for adapter in ADAPTERS:
        if adapter.can_handle(files):
            return adapter

    raise RuntimeError(
        "No language adapter could handle "
        "this repository."
    )
