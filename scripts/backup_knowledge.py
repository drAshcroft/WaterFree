"""Create and verify a consistent backup of the shared knowledge database."""
import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    source = Path.home() / '.waterfree/global/knowledge.db'
    folder = args.destination / ('WaterFree-knowledge-backup-' + datetime.now(ZoneInfo('America/Phoenix')).strftime('%Y%m%d-%H%M%S'))
    folder.mkdir()
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as original:
        with sqlite3.connect(folder / 'knowledge.db') as backup:
            original.backup(backup)
            integrity = backup.execute('PRAGMA integrity_check').fetchone()[0]
            if integrity != 'ok':
                raise RuntimeError(integrity)
            tables = [r[0] for r in backup.execute('SELECT name FROM sqlite_master WHERE type=?', ('table',))]
            counts = {table: backup.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0] for table in tables}
    manifest = {'source': str(source), 'backup': str(folder / 'knowledge.db'), 'integrity_check': integrity, 'table_counts': counts}
    (folder / 'BACKUP.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, indent=2))

if __name__ == '__main__':
    main()
