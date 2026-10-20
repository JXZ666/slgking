"""Run the unchanged web service locally with temporary, synthetic data only."""
from pathlib import Path
import argparse
import os
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--prepare-only', action='store_true',
                        help='Validate demo publication, then exit without listening.')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    source = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source))
    sys.dont_write_bytecode = True
    with tempfile.TemporaryDirectory(prefix='slgking-web-demo-') as temporary:
        state = Path(temporary)
        os.environ['LOCALAPPDATA'] = str(state / 'data')
        import slg_db
        import serve_catalog
        data = state / 'data' / 'slgking'
        data.mkdir(parents=True)
        with slg_db.session(str(data / 'slgking.db')) as conn:
            slg_db.upsert_game(conn, 'synthetic-preview-not-a-real-game',
                               'https://example.invalid/synthetic-preview',
                               '合成演示条目（不是真实游戏）',
                               version='demo', developer='Local preview')
        publish = state / 'publish'
        manifest = serve_catalog.publish(str(data), str(publish))
        if manifest.get('count') != 1:
            raise RuntimeError('Unexpected synthetic catalogue shape')
        if args.prepare_only:
            print('Synthetic catalogue validated; no listener or source requests.')
            return
        serve_catalog.Handler.publish_dir = str(publish)
        serve_catalog.Handler.data_dir = str(data)
        server = serve_catalog.CatalogueHTTPServer(
            ('127.0.0.1', args.port), serve_catalog.Handler)
        print(f'Local synthetic preview: http://127.0.0.1:{args.port}/m/#/')
        print('Ctrl+C to stop and delete temporary demo data.')
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == '__main__':
    main()
