<template>
  <div class="mx-auto flex min-h-[72vh] max-w-md flex-col items-center justify-center px-2">
    <div class="mb-8 flex flex-col items-center text-center">
      <div class="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-brand-500 to-brand-700 text-white shadow-lift shadow-brand-600/30">
        <BookOpen class="h-7 w-7" />
      </div>
      <h1 class="font-display text-2xl font-bold tracking-tight text-stone-900 sm:text-3xl">考研专业课备考助手</h1>
      <p class="mt-2 text-sm text-stone-500">真题追溯 · 主动回忆 · 考点频次 · 智能批改</p>
    </div>

    <div class="surface w-full p-8">
      <h2 class="text-base font-semibold text-stone-900">登录</h2>
      <p class="mt-1 text-xs text-stone-500">登录后继续你的学习记录</p>
      <form class="mt-6 space-y-4" @submit.prevent="submit">
        <div>
          <label class="field-label" for="login-username">用户名</label>
          <input id="login-username" v-model.trim="username" required autocomplete="username" class="field" />
        </div>
        <div>
          <label class="field-label" for="login-password">密码</label>
          <input id="login-password" v-model="password" required type="password" autocomplete="current-password" class="field" />
        </div>
        <p v-if="error" role="alert" class="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{{ error }}</p>
        <button type="submit" :disabled="loading" class="btn btn-primary w-full">{{ loading ? '登录中...' : '登录' }}</button>
      </form>
    </div>
  </div>
</template>
<script setup lang="ts">
import { ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { BookOpen } from 'lucide-vue-next';
import { useAuth } from '../composables/useAuth';
const username = ref(''); const password = ref(''); const error = ref('');
const router = useRouter(); const route = useRoute(); const auth = useAuth();
const loading = auth.loading;
const submit = async () => { error.value = ''; try { await auth.login(username.value, password.value); await router.replace(auth.user.value?.must_change_password ? { name: 'change-password', query: route.query } : String(route.query.redirect || '/today')); } catch { error.value = auth.errorMessage.value; } };
</script>
