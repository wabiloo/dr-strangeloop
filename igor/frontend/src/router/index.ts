import { createRouter, createWebHashHistory } from 'vue-router'
import ChannelList from '../views/ChannelList.vue'
import ChannelDetail from '../views/ChannelDetail.vue'
import ChannelNew from '../views/ChannelNew.vue'
import PlaylistList from '../views/PlaylistList.vue'
import PlaylistEditor from '../views/PlaylistEditor.vue'
import ArchiveImportList from '../views/ArchiveImportList.vue'
import ArchiveImportEditor from '../views/ArchiveImportEditor.vue'
import ManifestImportList from '../views/ManifestImportList.vue'
import ManifestImportEditor from '../views/ManifestImportEditor.vue'
import DocsView from '../views/DocsView.vue'

export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/channels' },
    { path: '/channels', name: 'channels', component: ChannelList },
    { path: '/channels/new', name: 'channel-new', component: ChannelNew },
    { path: '/channels/:name', name: 'channel-detail', component: ChannelDetail, props: true },
    { path: '/playlists', name: 'playlists', component: PlaylistList },
    { path: '/playlists/new', name: 'playlist-new', component: PlaylistEditor, props: { name: null } },
    { path: '/playlists/:name', name: 'playlist-edit', component: PlaylistEditor, props: true },
    { path: '/archives', name: 'archives', component: ArchiveImportList },
    { path: '/archives/:name', name: 'archive-import', component: ArchiveImportEditor, props: true },
    { path: '/manifests', name: 'manifests', component: ManifestImportList },
    { path: '/manifests/:name', name: 'manifest-import', component: ManifestImportEditor, props: true },
    { path: '/docs/:slug?', name: 'docs', component: DocsView, props: true },
  ],
})
