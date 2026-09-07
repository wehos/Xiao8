# Getting Started with Plugin Development

When you want N.E.K.O to do something new, a plugin is a straightforward place
to start. A plugin is a small Python project that describes itself in
`plugin.toml` and exposes features through a `NekoPluginBase` class. You do not
need to understand the whole N.E.K.O codebase before creating one.

## Start with two files

The CLI creates the usual project structure for you. At first, focus on these
two files:

```text
plugin/plugins/hello_world/
├── plugin.toml   # The plugin's ID, name, version, and Python class
└── __init__.py  # The features the plugin provides
```

`plugin.toml` tells N.E.K.O which plugin it is loading and where its Python
class lives. In `__init__.py`, decorators such as `@plugin_entry` expose the
features that users, the Agent, or the host can call.

A minimal feature looks like this:

```python
from plugin.sdk.plugin import NekoPluginBase, Ok, neko_plugin, plugin_entry


@neko_plugin
class HelloWorldPlugin(NekoPluginBase):
    @plugin_entry(id="hello", name="Hello", description="Say hello")
    async def hello(self, name: str = "World", **_):
        return Ok({"message": f"Hello, {name}!"})
```

Runtime entries are asynchronous and return `Ok(...)` or `Err(...)`. Other
capabilities—configuration, timers, lifecycle hooks, messages, storage, and UI—can
be added later when the plugin needs them.

## How a plugin runs

```text
plugin.toml → load the plugin class → register its entries → start the process
                                                       ↓
                                      invoke an entry → Ok / Err
```

There are two different kinds of "entry" in this flow. `[plugin].entry` in
`plugin.toml` points to the Python class that N.E.K.O loads. An ID declared by
`@plugin_entry`, such as `hello`, names a feature that can be called after the
plugin starts.

## Build your first plugin

1. Create it with `uv run neko-plugin init <plugin_id> --type plugin --name "<name>"`.
2. Open the generated `plugin.toml` and `__init__.py`.
3. Add or change an async `@plugin_entry` function.
4. Run `uv run neko-plugin check <plugin_id>` and the generated tests.
5. Refresh the plugin list in N.E.K.O's Plugins page, then start the plugin and invoke the entry. For later changes, reload the running plugin.
6. When it is ready to share, build a `.neko-plugin` package.

During development, treat packaged code and assets as read-only. Use
`self.config` for configuration, `self.data_path(...)` for persistent data, and
`self.cache_path(...)` for rebuildable cache.

The [Quick Start](./quick-start) walks through every step with a complete Hello
World plugin. After that, read [Plugin Config](./plugin-toml) and
[Entries & Parameters](./entries); the remaining pages are references you can
open when your plugin needs those capabilities.
