import json
import os

settings_path = 'c:\\OrpheusDL\\config\\settings.json'

with open(settings_path, 'r', encoding='utf-8') as f:
    data = json.load(f)

# Update album_format
data['global']['formatting']['album_format'] = "{main_artist}/({release_date}) {name} [{release}]{explicit}"

with open(settings_path, 'w', encoding='utf-8') as f:
    json.dump(data, f, indent=4)

print("Settings updated successfully.")
