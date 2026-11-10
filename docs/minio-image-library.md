# 秀人图片:用 MinIO 做图片来源

插件「秀人图片」不再抓网页,改为从 MinIO 读取图片。流程:

1. 你在 MinIO 里按自己的习惯上传、整理图片。
2. 有人发命令(图来 / 秀人 / 美图 / 随机图片)时,插件从**预读取池**里随机发一张,并扣积分。
3. 池子里剩下不超过 5 张时,后台自动再从 MinIO **随机预读取 20 张**(张数可配)。池子是该租户本地的临时缓存,**发过的图会从缓存删除**。
4. 不做去重和图片管理,同一张图重复出现没有关系。

## 一、部署 MinIO(由你操作)

### 放在哪台机器

只要求**运行机器人的 201 服务器的容器能访问到它**(局域网地址即可)。注意插件会拒绝 `127.0.0.1`、`localhost`、`169.254.*` 这类本机或链路本地地址,所以不能填本机回环地址。

### docker compose 参考

```yaml
services:
  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    restart: unless-stopped
    environment:
      MINIO_ROOT_USER: 换成你的管理员账号
      MINIO_ROOT_PASSWORD: 换成足够长的管理员密码
    ports:
      - "9000:9000"   # S3 接口,插件用这个
      - "9001:9001"   # 网页控制台,你上传图片用这个
    volumes:
      - ./minio-data:/data
```

### 镜像拉不下来时

201 访问不了 Docker Hub。在能联网的机器上:

```bash
docker pull minio/minio:latest
docker save minio/minio:latest | gzip > minio.tar.gz
# 拷到要部署的机器上
gunzip -c minio.tar.gz | docker load
```

## 二、建桶和只读账号

在控制台(`http://主机:9001`)里:

1. 创建桶,例如 `pics`。
2. 把图片传到桶里。建议放进一个前缀目录,例如 `xiuren/`,插件只读这个前缀下的图片。
3. **给插件单独建一个只读账号**,不要用管理员账号。Identity → Policies 新建策略,内容:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:ListBucket"],
      "Resource": ["arn:aws:s3:::pics", "arn:aws:s3:::pics/*"]
    }
  ]
}
```

   然后 Identity → Users 新建用户,绑定这条策略,再到该用户下创建一对 Access Key / Secret Key。

## 三、在插件里填写配置

后台「运营能力」里给机器人绑定「秀人图片」,配置项如下(密钥类不会回显,留空表示保持不变):

| 配置项 | 说明 | 示例 |
|---|---|---|
| `minio_endpoint` | MinIO 地址,**主机:端口,不要带 http://** | `192.168.2.32:9000` |
| `minio_bucket` | 桶名 | `pics` |
| `minio_prefix` | 只读这个前缀下的图片,留空读整个桶 | `xiuren/` |
| `minio_secure` | 地址是否走 https | 否 |
| `minio_region` | 区域,MinIO 默认即可 | `us-east-1` |
| `minio_access_key` | 只读账号的 Access Key(加密保存) | |
| `minio_secret_key` | 只读账号的 Secret Key(加密保存) | |
| `prefetch_count` | 每次预读取的张数,默认 20,最大 50 | `20` |
| `points_cost` | 每次发图扣的积分 | `5` |
| `max_image_bytes` | 单张图上限,默认 8MB,超过的会跳过 | |

支持的图片格式:jpg、jpeg、png、gif、webp。大小为 0 或超过上限的对象会被跳过。

## 四、常见问题

- **回复"图片来源还没有配置"**:endpoint、bucket、两个密钥有一项没填。
- **回复"图片正在准备中"**:池子是空的,已安排后台预读取,通常几十秒内就绪;如果一直这样,在后台看这个绑定的任务执行记录里的错误原因。
- **错误"无法连接 MinIO"**:201 的容器连不到该地址或端口(检查防火墙、地址是否写成了 `http://...`)。
- **错误"S3 返回 403"**:密钥不对,或策略没覆盖这个桶。
- **错误"MinIO 地址不能指向本机或链路本地地址"**:不能填 `127.0.0.1` / `localhost`,请填局域网 IP。
- 目前插件通过路径风格地址访问(`http://地址/桶/对象`),MinIO 默认就是这种,不需要额外设置。
