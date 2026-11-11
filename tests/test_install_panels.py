import importlib.util
from pathlib import Path
import pytest
spec=importlib.util.spec_from_file_location('panels',Path(__file__).parents[1]/'scripts/install_panels.py')
p=importlib.util.module_from_spec(spec);spec.loader.exec_module(p)

def test_private_management_and_separate_persistent_data():
    images={k:v+'@sha256:'+'a'*64 for k,v in p.IMAGES.items()}
    c=p.compose(['xui','wgdashboard'],images,'203.0.113.1')
    assert '127.0.0.1:2053:2053/tcp' in c['services']['xui']['ports']
    assert '127.0.0.1:10086:10086/tcp' in c['services']['wgdashboard']['ports']
    assert c['services']['wgdashboard']['cap_add']==['NET_ADMIN']
    assert len(c['volumes'])==5

@pytest.mark.parametrize('image',['ghcr.io/mhsanaei/3x-ui:latest','evil.example/panel@sha256:'+'a'*64])
def test_refuse_mutable_or_unofficial_image(image):
    with pytest.raises(ValueError):p.compose(['xui'],{'xui':image})

def test_wireguard_requires_public_endpoint_format():
    with pytest.raises(ValueError):p.compose(['wgdashboard'],{'wgdashboard':p.IMAGES['wgdashboard']+'@sha256:'+'a'*64},'bad')
