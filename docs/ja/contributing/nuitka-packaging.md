
# Nuitka パッケージング

追跡対象のパッケージング契約は、desktop workflow とその準備・検証スクリプトです。現在のリポジトリに追跡された `build_nuitka.bat` はないため、第二の正規入口として記載しないでください。

## Python package 名

import 可能な `.py` を含むディレクトリは underscore 名を使い、`__init__.py` を含めます。hyphen を含む import package は通常の Python 命名に反し、data inclusion とも相性が悪くなります。`tests/unit/test_no_hyphen_python_packages.py` がこの規則を検査します。

import 可能な Python を `--include-data-dir` で配布しません。Nuitka は data directory から code-like suffix を除外します。package は通常どおり compile し、明確な runtime 契約を持つ interpreted／sandboxed source payload だけを raw data として含めます。

## 組み込み plugin の staging

現在の desktop workflow は次を実行します。

1. `scripts/prepare_nuitka_plugins.py prepare`
2. 生成された `build_nuitka_launcher.py` を compile
3. `scripts/prepare_nuitka_plugins.py install` で build distribution へ install
4. `scripts/check_nuitka_dist.py <dist> --plugin-stage build/nuitka-plugins` を実行

Staging script は各 plugin の `[tool.neko.build]` 規則を適用し、選択的な exclusion を生成します。全量の `--include-data-dir=plugin/plugins=plugin/plugins` や `--nofollow-import-to=plugin.plugins` を復活させないでください。どちらも別の形で staging 契約を迂回します。

## Asset と dynamic import

新しい runtime asset／dynamic import では、次の協調変更が必要になる場合があります。

- cross-platform と Linux-only workflow の Nuitka include option
- plugin payload 用 `scripts/prepare_nuitka_plugins.py`
- `scripts/check_nuitka_dist.py` の必須 asset 検証
- 対象を絞った import／package test

Embedding と tiktoken asset は workflow で別々に準備・検証されます。

## 安全な診断

パッケージ版 launcher は複数 service を起動します。親だけを終了すると、`dist/Xiao8` を保持する process が残ることがあります。起動前に静的検査を優先します。

```bash
uv run python scripts/check_nuitka_dist.py dist/Xiao8 --plugin-stage build/nuitka-plugins
uv run pytest tests/unit/test_no_hyphen_python_packages.py -q
```

パッケージ実行が必要なら、正確な artifact／revision を記録し、再ビルド前に全 child service を停止します。古いロック済み distribution を未レビューの再帰削除で直さないでください。
