#### `data_migration_types.md`

# Technical Specifications - Data Migration

## 1. Data Migration Types

### Configuration Migration
```json
{
  "configuration_migration": {
    "preferences": {
      "ui_theme": "universal_theme_id",
      "keyboard_shortcuts": "universal_shortcut_format",
      "workspace_layout": "universal_layout_format"
    },
    "extensions": [
      {
        "id": "universal_extension_id",
        "settings": "extension_specific_config",
        "platform_mappings": {
          "windows": "windows_extension_id",
          "macos": "macos_extension_id",
          "linux": "linux_extension_id"
        }
      }
    ],
    "integrations": {
      "file_associations": "universal_file_type_mappings",
      "protocol_handlers": "universal_protocol_mappings",
      "system_services": "platform_specific_service_mappings"
    }
  }
}
