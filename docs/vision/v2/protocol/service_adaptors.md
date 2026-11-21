#### `service_adaptors.md`

# Windows Service Adapter
class WindowsServiceAdapter:
    def extract_registry_settings(self, app_name: str) -> dict:
        """Extract Windows registry settings for an application"""

    def extract_appdata_files(self, app_name: str) -> List[FileData]:
        """Extract application data from AppData folders"""

    def configure_file_associations(self, associations: List[FileAssociation]):
        """Configure Windows file associations"""

# macOS Service Adapter
class MacOSServiceAdapter:
    def extract_plist_preferences(self, app_bundle: str) -> dict:
        """Extract macOS plist preferences"""

    def extract_application_support(self, app_name: str) -> List[FileData]:
        """Extract data from Application Support folder"""

    def configure_launch_services(self, handlers: List[ProtocolHandler]):
        """Configure macOS Launch Services"""

# Linux Service Adapter
class LinuxServiceAdapter:
    def extract_config_files(self, app_name: str) -> List[ConfigFile]:
        """Extract configuration files from ~/.config"""

    def extract_desktop_entries(self, app_name: str) -> List[DesktopEntry]:
        """Extract .desktop files and system integrations"""

    def configure_mime_types(self, associations: List[MimeAssociation]):
        """Configure Linux MIME type associations"""
