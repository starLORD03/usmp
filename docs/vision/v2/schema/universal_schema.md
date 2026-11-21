#### `universal_schemas.md`

# Universal Service Schemas

## 1. Application Configuration Schema
```json
{
  "usmp_application": {
    "metadata": {
      "name": "string",
      "version": "string",
      "category": "enum[productivity, media, communication, development, system]",
      "platform_requirements": {
        "minimum_os_version": "string",
        "architecture": "enum[x86, x64, arm64, universal]",
        "dependencies": ["string"],
        "required_permissions": ["string"]
      }
    },
    "configuration": {
      "user_preferences": "object",
      "application_settings": "object",
      "ui_customizations": "object",
      "keyboard_shortcuts": "object",
      "plugin_configurations": "object"
    },
    "user_data": {
      "documents": ["file_reference"],
      "projects": ["project_reference"],
      "recent_files": ["file_reference"],
      "bookmarks": ["bookmark_object"],
      "custom_templates": ["template_object"]
    },
    "runtime_state": {
      "window_layouts": "object",
      "session_data": "object",
      "cached_data": "object",
      "temporary_files": ["file_reference"]
    },
    "integrations": {
      "system_integrations": ["integration_object"],
      "file_associations": ["association_object"],
      "protocol_handlers": ["protocol_object"],
      "context_menu_items": ["menu_item_object"]
    }
  }
}
