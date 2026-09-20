import { createRouter, createWebHashHistory } from 'vue-router'
import ChannelList from '../views/ChannelList.vue'
import ChannelDetail from '../views/ChannelDetail.vue'
import ChannelNew from '../views/ChannelNew.vue'
import PlaylistList from '../views/PlaylistList.vue'
import PlaylistEditor from '../views/PlaylistEditor.vue'

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
  ],
})
