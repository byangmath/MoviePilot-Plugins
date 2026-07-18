from pathlib import Path


class PathMapper:
    def __init__(self, raw_mappings: str = ""):
        self._mappings = self._parse(raw_mappings)

    @staticmethod
    def _normalise_prefix(value: str) -> str:
        value = value.strip().replace("\\", "/")
        return value.rstrip("/") or "/"

    @classmethod
    def _parse(cls, raw_mappings: str) -> list[tuple[str, str]]:
        mappings: list[tuple[str, str]] = []
        for line in (raw_mappings or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=>" not in line:
                continue
            source, target = line.split("=>", 1)
            source = cls._normalise_prefix(source)
            target = cls._normalise_prefix(target)
            if source and target:
                mappings.append((source, target))
        mappings.sort(key=lambda item: len(item[0]), reverse=True)
        return mappings

    def map(self, jellyfin_path: str) -> Path | None:
        if not jellyfin_path:
            return None
        normalised = jellyfin_path.replace("\\", "/")
        for source_prefix, target_prefix in self._mappings:
            if normalised == source_prefix or normalised.startswith(source_prefix + "/"):
                suffix = normalised[len(source_prefix):].lstrip("/")
                return Path(target_prefix) / suffix
        return None

    def has_mappings(self) -> bool:
        return bool(self._mappings)
