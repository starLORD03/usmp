#### `user_data_migration.md`

## 1. User Data Migration Schema
```json

{
  "user_data_migration": {
    "documents": [
      {
        "path": "original_path",
        "type": "file_type",
        "metadata": "file_metadata",
        "target_location": "platform_appropriate_path",
        "conversion_required": "boolean"
      }
    ],
    "application_state": {
      "open_documents": "session_state",
      "window_positions": "ui_state",
      "recent_history": "usage_history"
    },
    "custom_content": [
      {
        "type": "templates|snippets|macros|scripts",
        "content": "actual_content",
        "metadata": "content_metadata"
      }
    ]
  }
}
