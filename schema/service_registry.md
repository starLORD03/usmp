### `service_registry.md`

## Service Registry Schema

```json
{
  "service_registry": {
    "text_editor": {
      "universal_identifier": "usmp.productivity.text_editor",
      "providers": {
        "microsoft": {
          "services": {
            "visual_studio_code": {
              "platforms": ["windows", "macos", "linux"],
              "features": ["syntax_highlighting", "extensions", "git_integration"],
              "pricing": "free",
              "usmp_support": "native"
            }
          }
        },
        "sublime": {
          "services": {
            "sublime_text": {
              "platforms": ["windows", "macos", "linux"],
              "features": ["syntax_highlighting", "packages", "multiple_cursors"],
              "pricing": "license_required",
              "usmp_support": "plugin"
            }
          }
        }
      }
    }
  }
}