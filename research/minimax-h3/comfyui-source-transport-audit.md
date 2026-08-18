# ComfyUI 固定源码的中国网络替代传输核验

检索时间：2026-08-17（Asia/Shanghai）  
范围：仅审计 ComfyUI commit `0d80858061b511bd38c8cef4c235ef8e01040822` 的可追溯获取路径；未连接目标主机，未执行写操作。

## 已确认的官方事实

1. [Comfy-Org/ComfyUI 的官方 commit API](https://api.github.com/repos/Comfy-Org/ComfyUI/commits/0d80858061b511bd38c8cef4c235ef8e01040822) 确认该对象存在，commit 为 `0d80858061b511bd38c8cef4c235ef8e01040822`，其 Git tree 为 `e2791b95dc97f50ef97a22499131160b605edd47`。这两个值是传输后身份校验的锚点。
2. [ComfyUI 官方 Linux 手工安装文档](https://docs.comfy.org/installation/manual_install) 的源码安装方式是克隆 `https://github.com/Comfy-Org/ComfyUI.git`；文档没有提供 Linux 的官方独立安装包或第二个官方源码托管站。
3. [ComfyUI-Manager 官方配置文档](https://docs.comfy.org/manager/configuration) 将 `GITHUB_ENDPOINT` 定义为 GitHub 访问的反向代理，并明确给出示例：

   ```sh
   GITHUB_ENDPOINT=https://mirror.ghproxy.com/https://github.com
   ```

   因而该地址是官方文档列出的**访问代理**，而不是 Comfy-Org 发布的独立发行包或由 Comfy-Org 运营的源码镜像。

## ModelScope 与 Release 核验

- 对 `https://www.modelscope.cn/api/v1/models/Comfy-Org/ComfyUI` 的公开查询返回 404；未找到由 `Comfy-Org` 在 ModelScope 发布、且可证明含上述固定 commit 的 ComfyUI 源码仓库。
- ComfyUI GitHub 标签是版本标签；查询到的当前标签提交均不是该 commit。故不能把任一发布标签或第三方打包的 ComfyUI 当作该固定 revision 的等价物。
- GitHub 自动归档仍可用作**官方源码的归档形式**：`https://github.com/Comfy-Org/ComfyUI/archive/0d80858061b511bd38c8cef4c235ef8e01040822.tar.gz`。但它仍依赖 GitHub 连通性，且 GitHub 不为该自动归档发布独立、稳定的文件 SHA-256；不应仅凭下载文件哈希把它替代为可审计 Git revision。

## 可接受的条件化回退（需新计划审核）

若目标机对官方文档所列 `mirror.ghproxy.com` 可达，可将其仅作为到官方 GitHub 的传输代理，并仍锁定官方 commit：

```sh
git clone --no-checkout https://mirror.ghproxy.com/https://github.com/Comfy-Org/ComfyUI.git /home/super/wl/db/algo/H3/ComfyUI
git -C /home/super/wl/db/algo/H3/ComfyUI checkout --detach 0d80858061b511bd38c8cef4c235ef8e01040822
test "$(git -C /home/super/wl/db/algo/H3/ComfyUI rev-parse HEAD)" = 0d80858061b511bd38c8cef4c235ef8e01040822
test "$(git -C /home/super/wl/db/algo/H3/ComfyUI rev-parse HEAD^{tree})" = e2791b95dc97f50ef97a22499131160b605edd47
```

这保留了 `comfyui_official`（A 级）作为源码与身份来源；代理只是网络传输层。代理本身不能作为单独的 A 级源码证据。执行前须对目标机作只读连接预检；失败时不可静默换用不明 Git 镜像。

## 结论

未发现可作为同等权威替代品的官方 ModelScope 源码仓库、官方中国镜像或含该固定 commit 的官方 release asset。唯一有官方文档支持的替代**传输路径**是 `GITHUB_ENDPOINT` 示例中的 ghproxy 反向代理，且必须通过上述 commit 与 tree 校验。它应作为经过重新审核计划的条件化回退，而非自动替换当前 GitHub clone。
