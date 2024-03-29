import subprocess
import os

try:
    version = (
        subprocess.check_output(["git", "describe", "--abbrev=0", "--tags"])
        .strip()
        .decode("utf-8")
    )
except Exception as e:
    print(
        "Could not get version tag. Probably a PR or a branch. Defaulting to version 0"
    )
    version = "0"

with open("DEPENDENCIES", "r") as f:
    deps = f.readlines()

# Fix conda meta.yaml
with open("package/parameterize/meta.yaml", "r") as f:
    text = f.read()

text = text.replace(
    "DEPENDENCY_PLACEHOLDER",
    "".join(["    - {}\n".format(dep.strip()) for dep in deps]),
)

with open("package/parameterize/meta.yaml", "w") as f:
    f.write(text)

# Set version in version file for __version__ variable
with open("parameterize/version.py", "r") as f:
    filedata = f.read()
# Replace the target string
filedata = filedata.replace("unpackaged", version)
# Write the file out again
with open("parameterize/version.py", "w") as f:
    f.write(filedata)

