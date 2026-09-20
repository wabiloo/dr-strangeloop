import { createRouter, createWebHashHistory } from 'vue-router'
import ChannelList from '../views/ChannelList.vue'
import ChannelDetail from '../views/ChannelDetail.vue'
import ChannelNew from '../views/ChannelNew.vue'
import ConfigList from '../views/ConfigList.vue'
import ConfigEditor from '../views/ConfigEditor.vue'

export default createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/channels' },
    { path: '/channels', name: 'channels', component: ChannelList },
    { path: '/channels/new', name: 'channel-new', component: ChannelNew },
    { path: '/channels/:name', name: 'channel-detail', component: ChannelDetail, props: true },
    { path: '/configs', name: 'configs', component: ConfigList },
    { path: '/configs/new', name: 'config-new', component: ConfigEditor, props: { name: null } },
    { path: '/configs/:name', name: 'config-edit', component: ConfigEditor, props: true },
  ],
})
