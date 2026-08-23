from pathlib import Path


path = Path("core/pipeline.py")
text = path.read_text()


old = '''    adapter = detect_adapter(
        repository_files
    )

    print(
        f"Detected language adapter: "
        f"{adapter.name}"
    )
'''

new = '''    adapter = detect_adapter(
        repository_files
    )

    build_command = adapter.build_command(
        repository_files
    )

    test_command = adapter.test_command(
        repository_files
    )

    print(
        f"Detected language adapter: "
        f"{adapter.name}"
    )

    print(
        f"Build command: {build_command}"
    )

    print(
        f"Test command: {test_command}"
    )
'''

if old not in text:
    raise SystemExit(
        "Could not find adapter detection block."
    )

text = text.replace(
    old,
    new,
    1
)


old = '''    if not run_expected_red_phase(
        config,
        workspace,
        state,
        contract[
            "test_snapshot"
        ]
    ):
'''

new = '''    if not run_expected_red_phase(
        config,
        workspace,
        state,
        contract[
            "test_snapshot"
        ],
        test_command
    ):
'''

if old not in text:
    raise SystemExit(
        "Could not find expected red call."
    )

text = text.replace(
    old,
    new,
    1
)


old = '''    if not run_build_phase(
        config,
        workspace,
        task,
        implementation_changes
    ):
'''

new = '''    if not run_build_phase(
        config,
        workspace,
        task,
        implementation_changes,
        build_command
    ):
'''

if old not in text:
    raise SystemExit(
        "Could not find build phase call."
    )

text = text.replace(
    old,
    new,
    1
)


old = '''    if not run_test_phase(
        config,
        workspace,
        task,
        state,
        planning["grouped"],
        implementation_changes
    ):
'''

new = '''    if not run_test_phase(
        config,
        workspace,
        task,
        state,
        planning["grouped"],
        implementation_changes,
        test_command
    ):
'''

if old not in text:
    raise SystemExit(
        "Could not find test phase call."
    )

text = text.replace(
    old,
    new,
    1
)

path.write_text(text)

print(
    "Adapter build/test commands connected "
    "to pipeline."
)
