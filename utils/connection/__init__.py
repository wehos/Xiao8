"""First-party platform connectors (the app's core body).

Each subpackage is a dependency-light transport/connector library that is
imported by plugins and run in-process. These are not plugins themselves --
they are core-maintained infrastructure shipped with the app. The QQ connector
lives in :mod:`utils.connection.qq`.
"""
