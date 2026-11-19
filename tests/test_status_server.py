import urllib.error
import urllib.request

import pytest

from workshop.reports.status_server import StatusServer


def test_serves_only_the_status_page_on_localhost(tmp_path):
    page = tmp_path / 'status.html'
    page.write_text('<h1>车间状态</h1>', encoding='utf-8')
    with StatusServer(page, 'http://127.0.0.1:0/status.html') as server:
        base = f'http://127.0.0.1:{server.port}'
        assert '车间状态' in urllib.request.urlopen(base + '/status.html').read().decode()
        assert urllib.request.urlopen(base + '/status.html#%E5%A4%84%E7%90%86%E4%B8%8D%E4%BA%86').status == 200
        (tmp_path / 'secret.txt').write_text('x')
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + '/secret.txt')
        assert error.value.code == 404
        page.unlink()
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(base + '/status.html')
        assert error.value.code == 503


@pytest.mark.parametrize('url', ['http://0.0.0.0:8765/status.html', 'https://127.0.0.1:8765/', 'http://127.0.0.1/status.html'])
def test_refuses_anything_but_localhost_with_a_port(tmp_path, url):
    with pytest.raises(ValueError):
        StatusServer(tmp_path / 'status.html', url)
