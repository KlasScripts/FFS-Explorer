"""The bundle-id map reads only app containers' own metadata plists
(adapters.ffs._app_container_plists): <Containers>/Data/Application,
Data/PluginKitPlugin or Shared/AppGroup, the GUID folder, the plist directly
in it."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'app'))

from adapters.ffs import _app_container_plists

M = '.com.apple.mobile_container_manager.metadata.plist'
G = '0A1B2C3D-4E5F-6071-8293-A4B5C6D7E8F9'


def test_only_app_container_plists_are_read():
    pre = 'filesystem2/mobile/Containers/'
    names = [
        f'{pre}Data/Application/{G}/{M}',                     # yes
        f'{pre}Data/PluginKitPlugin/{G}/{M}',                 # yes
        f'{pre}Shared/AppGroup/{G}/{M}',                      # yes
        f'{pre}Data/InternalDaemon/{G}/{M}',                  # a system daemon
        f'{pre}Data/Application/{G}/Library/{G}/{M}',         # deeper in an app
        f'{pre}Data/Application/NotAGuid/{M}',                # not a GUID folder
        f'{pre}Data/Application/{G}/x{M}',                    # another name
        f'filesystem2/mobile/Library/{G}/{M}',                # outside Containers
    ]
    assert sorted(_app_container_plists(names, pre)) == sorted(names[:3])
    gk = '/private/var/mobile/Containers/'
    assert _app_container_plists([f'{gk}Shared/AppGroup/{G}/{M}'], gk) == [f'{gk}Shared/AppGroup/{G}/{M}']
