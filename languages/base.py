from abc import ABC, abstractmethod


class LanguageAdapter(ABC):
    name = "generic"

    @abstractmethod
    def can_handle(self, files):
        raise NotImplementedError

    @abstractmethod
    def build_command(self, workspace_files):
        raise NotImplementedError

    @abstractmethod
    def test_command(self, workspace_files):
        raise NotImplementedError

    def classify_red_state(self, output):
        return None

    def extract_failure_paths(self, output):
        return []

    def is_test_path(self, path):
        lower = path.lower()

        return (
            "test" in lower
            or "spec" in lower
        )

    def is_config_path(self, path):
        return False

    def describe(self):
        return {
            "name": self.name
        }
