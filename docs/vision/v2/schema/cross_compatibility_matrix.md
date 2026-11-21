### `cross_compatibility_matrix.md`

## Cross Compatibility Matrix
```json
{
  "compatibility_matrix": {
    "service_type": "text_editor",
    "platforms": {
      "windows": {
        "native_apps": ["Notepad++", "Visual Studio Code", "Sublime Text"],
        "compatibility_layer": "WSL for Linux apps",
        "data_formats": ["txt", "rtf", "md", "json", "xml"]
      },
      "macos": {
        "native_apps": ["TextEdit", "Visual Studio Code", "Sublime Text"],
        "compatibility_layer": "Rosetta 2 for Intel apps",
        "data_formats": ["txt", "rtf", "md", "pages", "json"]
      },
      "linux": {
        "native_apps": ["gedit", "vim", "Visual Studio Code", "emacs"],
        "compatibility_layer": "Wine for Windows apps",
        "data_formats": ["txt", "md", "json", "xml", "tex"]
      },
      "android": {
        "native_apps": ["Google Docs", "Microsoft Word", "WPS Office"],
        "compatibility_layer": "Android app sandbox",
        "data_formats": ["txt", "docx", "md", "pdf"]
      },
      "ios": {
        "native_apps": ["Notes", "Microsoft Word", "Google Docs"],
        "compatibility_layer": "iOS app sandbox",
        "data_formats": ["txt", "docx", "pages", "pdf"]
      }
    },
    "cross_platform_services": {
      "direct_equivalents": ["Visual Studio Code", "Sublime Text"],
      "functional_equivalents": [
        {
          "windows": "Notepad++",
          "macos": "TextEdit",
          "linux": "gedit",
          "feature_parity": 0.85
        }
      ]
    }
  }
}