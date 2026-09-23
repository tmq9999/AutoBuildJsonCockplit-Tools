from html.parser import HTMLParser


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.elements = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


def test_local_ui_shell_is_served_with_controls_and_no_external_assets(client):
    response = client.get('/')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/html')
    document = Document(response.text)
    by_id = {attrs['id']: attrs for _, attrs in document.elements if 'id' in attrs}
    for field in ['admin-token','accounts-text','accounts-file','proxies-text','mode','workers','timeout',
                  'start-btn','stop-btn','status-filter','manual-email','callback-url','login-form']:
        assert field in by_id
    assert by_id['admin-token']['type'] == 'password'
    assert 'disabled' in by_id['start-btn'] and 'disabled' in by_id['stop-btn']
    labels = {attrs['for'] for tag, attrs in document.elements if tag == 'label' and 'for' in attrs}
    assert {'admin-token','accounts-text','accounts-file','proxies-text','workers','callback-url'} <= labels
    exports = {attrs['data-export'] for _, attrs in document.elements if 'data-export' in attrs}
    assert exports == {'success','errors','phone_verify','filtered','cancelled'}
    for tag, attrs in document.elements:
        if tag == 'script' or (tag == 'link' and attrs.get('rel') == 'stylesheet'):
            path = attrs.get('src') or attrs.get('href')
            assert path and path.startswith('/static/')
            asset = client.get(path)
            assert asset.status_code == 200
            assert asset.headers['cache-control'] == 'no-store'
    assert "script-src 'self'" in response.headers['content-security-policy']


def test_static_paths_do_not_expose_config_or_sources(client):
    for path in ['/static/../security.py','/static/%2e%2e/security.py','/data/local-config.json','/.env','/static/not-found']:
        assert client.get(path).status_code == 404
