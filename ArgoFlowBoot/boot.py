#!/usr/bin/python3
# -*- coding: utf-8 -*-

import json
import os
import re
from K8sBoot.boot import Boot as K8sBoot
from pyutilb.util import *
from pyutilb.file import *
from pyutilb.cmd import *
from pyutilb import YamlBoot, BreakException
from pyutilb.log import log
from ArgoFlowBoot.task_namer import *

'''
代理工件对象, 并改写tostring(), 以便支持
1  ${@art.path} 输出工件对象属性
2. $@art 输出如 {{inputs.artifacts.source}}
'''
class ArtifactProxy(dict):

    def __init__(self, data, repr):
        super().__init__(data or {})
        self.repr = repr # 如 {{inputs.artifacts.source}}

    def __repr__(self):
        return self.repr

'''
argo flow配置生成的基于yaml的启动器
'''
class Boot(YamlBoot):

    def __init__(self, output_dir):
        super().__init__()
        self.output_dir = os.path.abspath(output_dir or 'out')
        # step_dir作为当前目录
        self.step_dir_as_cwd = True
        # 不需要输出统计结果到stat.yml
        self.stat_dump = False
        # 动作映射函数
        actions = {
            'flow_template': self.flow_template,
            'flow': self.flow,
            'labels': self.labels,
            'spec': self.spec,
            'args': self.args,
            'cron': self.cron,
            'vc_templates': self.vc_templates,
            'templates': self.templates,
        }
        self.add_actions(actions)

        # 任务命名者
        self.namer = FuncIncrTaskNamer()

        # 模板主题构建器
        self.template_body_builders = {
            'container': self.build_container,
            'sidecars': self.build_sidecars,
            'script': self.build_script,
            'steps': self.build_steps,
            'suspend': self.build_suspend,
            'apply': self.build_apply,
            'delete': self.build_delete,
            'dag': self.build_dag,
        }
        # python版本
        py_versions = '3.6/3.7/3.8/3.9/3.10/3.11'.split('/')
        for version in py_versions:
            self.template_body_builders['python'+version] = self.wrap_build_python(version)

        # flow作用域的属性，跳出flow时就清空
        self._flow = '' # 流程名
        self._labels = {}  # 记录标签
        self._spec = {}  # 记录流程其他配置
        self._args = None  # 记录流程级传参
        self._cron_spec = None  # 记录cron选项
        self._templates = {}  # 记录模板，key是模板名，value是模板定义
        self._template_inputs = {} # 记录模板的输入参数名
        self._template_outputs = {} # 记录模板的输出参数名
        self._vc_templates = None # 记录vs模板
        self._vc_mounts = [] # 记录vs挂载路径

        # 跨flow的属性
        self._flow2template_inputs = {}  # 记录所有流程的模板输入参数名

    # 清空app相关的属性
    def clear_app(self):
        self._flow = None  # 流程名
        set_var('flow', None)
        self._labels = {}  # 记录标签
        self._spec = {}  # 记录流程其他配置
        self._args = None  # 记录流程级传参
        self._cron_spec = None  # 记录cron选项
        self._templates = {}  # 记录模板，key是模板名，value是模板定义
        self._template_inputs = {} # 记录模板的输入参数名
        self._template_outputs = {} # 记录模板的输出参数名
        self._vc_templates = None # 记录vs模板
        self._vc_mounts = [] # 记录vs挂载路径
        clear_vars('*') # 清理全部变量
        self.namer = FuncIncrTaskNamer() # 重置命名器，因为他内部有状态(计数)

    def save_yaml(self, data):
        '''
        保存yaml
        :param data 资源数据
        '''
        # 检查流程名
        if self._flow is None:
            raise Exception(f"生成工作流文件失败: 没有指定流程")
        file = f"{self._flow}.yml"
        # 转yaml
        if isinstance(data, list): # 多个资源
            data = list(map(yaml.dump, data))
            data = "\n---\n\n".join(data)
        elif not isinstance(data, str):
            data = yaml.dump(data, sort_keys=False)
        # 创建目录
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
        # 保存文件
        file = os.path.join(self.output_dir, file)
        write_file(file, data)

    def print_submit_cmd(self):
        '''
        打印 kubectl apply 命令
        '''
        cmd = f'流程[{self._flow}]的定义文件已生成完毕, 如要提交到到集群中请手动执行: argo submit {self.output_dir}/{self._flow}.yml'
        log.info(cmd)

    # --------- 动作处理的函数 --------
    def flow_template(self, steps, name=None):
        self.flow(steps, name, True)

    def flow(self, steps, name=None, is_flow_template=False):
        '''
        声明工作流，并执行子步骤
        :param steps 子步骤
        name 流程名
        '''
        # app名可带参数
        name = replace_var(name)
        self._flow = name
        set_var('flow', name)
        self._labels = {
            'flow': name
        }
        # 执行子步骤
        self.run_steps(steps)
        if self._cron_spec is None: # 生成flow
            yaml = self.build_flow(is_flow_template)
        else: # 生成cron flow
            yaml = self.build_cron_flow()
        self.save_yaml(yaml)
        # 记录所有流程的模板输入参数名
        self._flow2template_inputs[name] = self._template_inputs
        # 打印 kubectl apply 命令
        self.print_submit_cmd()
        # 清空app相关的属性
        self.clear_app()

    @replace_var_on_params
    def labels(self, lbs):
        '''
        设置流程标签
        :param lbs:
        :return:
        '''
        self._labels.update(lbs)

    def build_labels(self, lbs = None):
        if not lbs:
            return self._labels
        # 合并标签
        return dict(lbs, **self._labels)

    def build_flow(self, is_flow_template):
        '''
        构建 Workflow/WorkflowTemplate
        :param is_flow_template: 是否WorkflowTemplate
        :return:
        '''
        # 入口为main
        entrypoint = "main"
        if entrypoint not in self._templates:
            raise Exception("未定义入口模板: main")
        # 退出处理
        exit_handler = None
        if 'onexit' in self._templates:
            exit_handler = 'onexit'
        # 资源类型+元数据根据是否WorkflowTemplate有不同
        if is_flow_template:
            kind = "WorkflowTemplate"
            meta = {"name": self._flow} # flow template固定名字
        else:
            kind = "Workflow"
            meta = { "generateName": self._flow + '-' } # flow自动生成名字
        meta["labels"] = self.build_labels()
        yaml = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": kind,
            "metadata": meta,
            "spec": {
                "entrypoint": entrypoint,
                "onExit": exit_handler,
                "arguments": self._args,
                **self._spec,
                "volumeClaimTemplates": self._vc_templates,
                "templates": list(self._templates.values()),
                # "ttlStrategy": {
                #     "secondsAfterCompletion": 300
                # },
                # "podGC": {
                #     "strategy": "OnPodCompletion"
                # }
            }
        }
        del_dict_none_item(yaml["spec"])
        return yaml

    def build_cron_flow(self):
        '''
        构建 CronWorkflow
           cron选项调用 k8sboot 来生成
           流程选项调用 build_flow() 来生成
        :return:
        '''
        yaml = {
            "apiVersion": "argoproj.io/v1alpha1",
            "kind": "CronWorkflow",
            "metadata": {
                "name": self._flow,
                "labels": self.build_labels()
            },
            "spec": {
                **self._cron_spec,
                "workflowSpec": self.build_flow(False)["spec"]
            }
        }
        return yaml

    @replace_var_on_params
    def vc_templates(self, vcs):
        '''
        构建持久卷声明
        :params vcs dict, key是vc模板名, value是{size, mount}
                       size是pvc的存储空间大小
                       mount可以是str，表示整体挂载到容器中的路径名，也可以是dict，key是pvc子路径，value是挂载到容器内路径
                       如 work: {mount: /work, size: 1Gi} 定义名为work的pvc模板，请求了 1GB 的存储空间，并挂载到容器的/work
        '''
        if vcs is None or len(vcs) == 0:
            return None
        self._vc_templates = []
        for name, option in vcs.items():
            # 先去掉mount, 下一步处理
            mounts = get_and_del_dict_item(option, 'mount')
            # 1 构建vc
            vc = {
                "metadata": {
                    "name": name
                },
                "spec": {
                    "accessModes": get_and_del_dict_item(option, "accessModes", ["ReadWriteOnce"]), # 访问模式
                    "resources": {
                        "requests": {
                            "storage": get_and_del_dict_item(option, 'size', '100Mi')  # 空间大小， 默认100M
                        }
                    },
                    **option
                }
            }
            self._vc_templates.append(vc)

            # 2 处理mount
            # str，表示整体挂载到容器中的路径名
            # dict，key是pvc子路径，value是挂载到容器内路径
            if isinstance(mounts, str):
                mounts = {'': mounts}
            for sub_path, mount_path in mounts.items():
                mount = {
                    "name": name,
                    "mountPath": mount_path
                }
                if sub_path:
                    mount['subPath'] = sub_path
                self._vc_mounts.append(mount)

            # 设变量
            key1 = get_dict_first_key(mounts)
            if key1 == '': # 整体挂载
                set_var(name, mounts[key1])
            else: # 子路径挂载
                set_var(name, mounts)

    # 流程的其他配置
    @replace_var_on_params
    def spec(self, option):
        self._spec = option

    @replace_var_on_params
    def args(self, args):
        '''
        流程级传参, 参考 https://argoproj.github.io/argo-workflows/walk-through/parameters/
        :param args:
        :return:
        '''
        self._args = self.build_dict_args(args, 'flow-args')

    # 定时
    @replace_var_on_params
    def cron(self, option):
        if isinstance(option, str):
            option = {
                'schedule': option
            }
        # 调用k8sboot来构建cron选项
        self._cron_spec = K8sBoot('.').build_cron(option)

    def templates(self, options):
        # 模板名:模板配置
        for name, option in options.items():
            self.build_template(name, option)

    def build_template_body(self, option):
        ret = {}
        # 逐个匹配模板类型，并调用对应的模板主体构建器
        for type, builder in self.template_body_builders.items():
            if type in option:
                part = builder(get_and_del_dict_item(option, type))
                ret.update(part)

        return ret

    # 获得默认镜像
    def get_default_image(self, option):
        cmd = option.get('command')
        if 'source' in option and cmd is None:
            return 'bash'

        if cmd is None:
            return 'alpine'

        if isinstance(cmd, list):
            cmd = cmd[0]
        cmd = cmd.strip()
        if cmd == 'bash':
            return 'bash'

        if cmd.startswith('python'):
            version = re.search(r'^python([\d\.]+)?', cmd).group(1) or '3.6' # 从命令中获得python版本，缺省为3.6
            return f"python:alpine{version}"

        if 'cowsay ' in cmd or 'cowsay ' == cmd :
            return 'docker/whalesay'

        if 'curl ' in cmd or 'curl ' == cmd :
            return 'appropriate/curl'

        return 'alpine'

    # 构建容器模板
    def build_container(self, option):
        return {
            "container": self.build_container_body(option),
        }

    def build_container_body(self, option):
        # imagePullPolicy置空
        # if 'imagePullPolicy' not in option:
        #     option['imagePullPolicy'] = None
        # 默认镜像
        if 'image' not in option:
            option['image'] = self.get_default_image(option)
        # 调用k8sboot来构建容器
        ret = K8sBoot('.').build_container(None, option)
        # 添加vc模板的挂载
        if self._vc_mounts:
            if "volumeMounts" not in ret:
                ret["volumeMounts"] = []
            ret["volumeMounts"] += self._vc_mounts
        return ret

    def build_script(self, option):
        '''
        构建script模板，参考 https://argoproj.github.io/argo-workflows/walk-through/scripts-and-results/
        :param option:
        :return:
        '''
        # 默认命令
        if 'command' not in option:
            option['command'] = "bash"
        if isinstance(option['command'], str):
            option['command'] = [option['command']]
        # 源码
        if 'file' in option:
            option["source"] = read_file(get_and_del_dict_item(option, "file"))
        return {
            "script": self.build_container_body(option)
        }

    def wrap_build_python(self, version):
        def wrapper(option):
            option['command'] = f'python{version}'
            return self.build_script(option)
        return wrapper

    # 构建边车模板
    def build_sidecars(self, option: dict):
        containers = []
        for name, container in option.items():
            container = self.build_container_body(container)
            container['name'] = name
            containers.append(container)
        ret = {
            "sidecars": containers
        }
        return ret

    def build_template(self, name: str, option: dict):
        '''
        构建任务模板
        :param name: 任务模板名，函数调用的形式
        :param option: 任务模板选项
        :return:
        '''
        push_vars_stack() # 变量入栈
        # 解析函数调用
        name, args = parse_func(name, True)
        # 构建输入
        inputs = None
        if args: # 函数调用形式的入参
            inputs = self.build_list_args(args, 'inputs') # 构建输入，会增加变量
        else: # 显示定义`in`的入参
            ins = get_and_del_dict_item(option, 'in')
            if ins:
                inputs = self.build_dict_args(ins, 'inputs') # 构建输入，会增加变量
        # 记录模板的输入参数名
        # self._template_inputs[name] = args # wrong: args太复杂了，可能用=带参数默认值，可能用dict => 从inputs中解析
        self._template_inputs[name] = get_and_del_dict_item(inputs, 'name')
        if 'steps' not in option: # steps延迟替换变量, 因为下一步的输入变量会依赖上一步的输出
            option = replace_var(option, False) # 替换变量
        # 构建主体
        body = self.build_template_body(option)
        if body is None:
            raise Exception(f'不确定任务[{name}]的类型')
        # 构建输出
        out = get_and_del_dict_item(option, 'out')
        outputs = self.build_dict_args(out, 'outputs') # 构建输出
        if out: # 记录模板的输出参数名
            self._template_outputs[name] = out.keys()
        tpl = {
            "name": name,
            "inputs": inputs,
            **body,
            "outputs": outputs,
            **option
        }
        pop_vars_stack(False) # 变量出栈
        del_dict_none_item(tpl)
        self._templates[name] = tpl

    # 构建输入变量
    def build_input_vars(self, type, k, v=None):
        if k.startswith('@'):  # artifacts
            if type == 'flow-args':
                set_var(k, ArtifactProxy(v, '{{workflow.artifacts.' + k[1:] + '}}')) # {{workflow.artifacts.art_name}}
            elif type == 'inputs' and get_var(k, False) is None:
                set_var(k, ArtifactProxy(v, '{{inputs.artifacts.' + k[1:] + '}}')) # {{inputs.artifacts.source}}
            return

        # parameters
        if type == 'flow-args':
            set_var(k, '{{workflow.parameters.' + k + '}}')  # {{workflow.parameters.parameter_name}}
        elif type == 'inputs':
            set_var(k, '{{inputs.parameters.' + k + '}}') # {{inputs.parameters.message}}

    def check_input_args_order(self, names: list, type: str):
        '''
        检查参数的顺序: artifacts只能定义在parameters后面
        :param names: 参数名
        :param type: 参数类型: inputs/outputs/flow-args/call(调用模板)
        '''
        if type == 'inputs' or type == 'flow-args':
            art_exist = False # artifacts是否出现过
            for name in names:
                if name.startswith('@'):  # artifacts
                    art_exist = True
                    continue

                # parameters
                if art_exist:
                    raise Exception(f"入参定义{names}, 不符合规范: artifacts只能定义在parameters后面")

    def build_dict_args(self, args: dict, type: str):
        '''
        构建 outputs/flow args/call(调用模板) 的dict类型的参数
        输入参数参考 https://argoproj.github.io/argo-workflows/walk-through/parameters/
        输出参数参考 https://argoproj.github.io/argo-workflows/walk-through/output-parameters/

        :param args:
        :param type: 参数类型: inputs/outputs/flow-args/call(调用模板)
        :return:
        '''
        if not args:
            return None
        # 检查参数的顺序
        self.check_input_args_order(args.keys(), type)
        # 拆分parameters与artifacts
        params = {}
        arts = {}
        names = []
        for k, v in args.items():
            names.append(k)
            if k.startswith('@'): # artifacts
                v = self.fix_artifact_option(v, k)
                arts[k] = v
            else: # parameters
                v = replace_var(v, False)
                params[k] = v
            # 设输入变量
            self.build_input_vars(type, k, v)
        # 检查入参的顺序
        if type == 'inputs':
            self.check_input_args_order(names, type)
        # 构建参数
        ret = {
            "parameters": self.build_params(params),
            "artifacts": self.build_artifacts(arts, type),
        }
        del_dict_none_item(ret)
        # 返回参数名，方便后续记录模板入参
        if type == 'inputs':
            ret["name"] = names
        return ret

    def build_list_args(self, args: list, type: str):
        '''
        构建 inputs 的list类型的参数
        输入参数参考 https://argoproj.github.io/argo-workflows/walk-through/parameters/
        输出参数参考 https://argoproj.github.io/argo-workflows/walk-through/output-parameters/

        :param args:
        :param type: 参数类型: inputs/outputs/flow-args/call(调用模板)
        :return:
        '''
        if not args:
            return None
        # 拆分parameters与artifacts
        params = {}
        arts = {}
        names = []
        for arg in args:
            # 如果参数带默认值，则拆分参数名与参数值
            if '=' in arg:
                k, v = arg.split('=')
            else:
                k = arg
                v = None
            names.append(k)
            if k.startswith('@'):  # artifacts
                v = self.fix_artifact_option(v, k)
                arts[k] = v
            else: # parameters
                v = replace_var(v)
                params[k] = v
            # 设输入变量
            self.build_input_vars(type, k, v)
        # 检查入参的顺序
        if type == 'inputs':
            self.check_input_args_order(names, type)
        # 构建参数
        ret = {
            "parameters": self.build_params(params),
            "artifacts": self.build_artifacts(arts, type),
        }
        del_dict_none_item(ret)
        # 返回参数名，方便后续记录模板入参
        if type == 'inputs':
            ret["name"] = names
        return ret

    def fix_artifact_option(self, v, k):
        if not v:
            v = {}
        else: # 解析变量，有可能他依赖于前一个参数
            v = replace_var(v, False)

        # 路径
        if isinstance(v, str):
            path = v
            if ':' in path:
                path, mode = path.split(':', 1)
                v = {'path': path, 'mode': mode}
            else:
                v = {'path': path}

        # 未指定path，则取 /tmp/工件名
        if not v.get('path'):
            v['path'] = '/tmp/artifacts/' + k.replace('@', '')
        return v

    def build_artifacts(self, option, type=None):
        '''
        构建模板的工件参数
        :param option: dict(参数名, 工件信息)，其中工件信息包含挂载路径+存储信息，参考 ArgoFlowBoot/example/artifact-type.yml，支持自带文件存储信息(git/HTTP/GCS/S3)
                      https://argoproj.github.io/argo-workflows/walk-through/artifacts/
                      https://argoproj.github.io/argo-workflows/walk-through/hardwired-artifacts/
        :param type: 参数类型: inputs/outputs/flow-args/call(调用模板)
        :return:
        '''
        if not option:
            return None

        if isinstance(option, list):
            return [self.build_artifact(v, None, type) for v in option]

        if isinstance(option, dict):
            return [self.build_artifact(k, v, type) for k, v in option.items()]

        raise Exception(f"无效参数选项: {option}")

    def build_artifact(self, key, value=None, type=None):
        '''
        构建工件
        :param key:
        :param value: 挂载路径+存储信息，参考 ArgoFlowBoot/example/artifact-type.yml，支持自带文件存储信息(git/HTTP/GCS/S3)
                      https://argoproj.github.io/argo-workflows/walk-through/artifacts/
                      https://argoproj.github.io/argo-workflows/walk-through/hardwired-artifacts/
        :param type: 参数类型: inputs/outputs/flow-args/call(调用模板)
        :return:
        '''
        key = key.replace('@', '')

        # 如果用户没填，则默认用流程级同名参数
        if not value:
            return {
                "name": key,
            }

        # 用户有填
        # 1 有明细配置dict
        if isinstance(value, dict):
            ret = {
                "name": key,
                **value
            }
        else: # 2 单值
            ret = {
                "name": key,
                "path": value
            }

        # 3 对调用模板要修正属性: path转from, 如 from: "{{steps.generate-artifact.outputs.artifacts.etc}}"
        if type == 'call' and 'path' in ret:
            ret['from'] = get_and_del_dict_item(ret, 'path')
        # 4 对输出修正属性: expression转fromExpression, 如 fromExpression: "steps['flip-coin'].outputs.result == 'heads' ? steps.heads.outputs.artifacts.headsresult : steps.tails.outputs.artifacts.tailsresult"
        elif type == 'outputs' and 'expression' in ret:
            ret['fromExpression'] = get_and_del_dict_item(ret, 'expression')

        # 修正表达式
        if 'fromExpression' in ret:
            ret['fromExpression'] = self.fix_expression(ret['fromExpression'])
        return self.fix_expression(ret)

    def fix_expression(self, expr):
        '''
        如果expression的值用了变量，如 ${flip-coin.result} == 'heads' ? ${heads.result} : ${tails.result}
        会替换为 {{steps.flip-coin.outputs.result}} == ''heads'' ? {{steps.heads.outputs.result}} : {{steps.tails.outputs.result}}
        而我想要的是 steps['flip-coin'].outputs.result == 'heads' ? steps.heads.outputs.result : steps.tails.outputs.result
        => 1 非正常的变量命名，如包含-，如flip-coin，不能用.访问，需用[]来访问
           2 干掉 {{ 与 }}
        :param expr:
        :return: 
        '''
        # 1 解析出 {{ 与 }} 包含的内容
        # 2 对 steps.flip-coin.outputs 或 tasks.flip-coin.outputs 中间一段 .flip-coin 替换为 ['flip-coin']
        # re.sub(r'\{\{[^\}]\}\}')
        return expr.replace('{{', '').replace('}}', '')

    # 构建模板的输入参数
    def build_params(self, option):
        '''

        :param option:
        :param type: 参数类型: inputs/outputs/flow-args/call(调用模板)
        :return:
        '''
        if not option:
            return None

        if isinstance(option, list):
            return [self.build_param(v) for v in option]

        if isinstance(option, dict):
            return [self.build_param(k, v) for k, v in option.items()]

        raise Exception(f"无效参数选项: {option}")

    def build_param(self, key, value=None):
        if not value:
            return {
                "name": key
            }
        if isinstance(value, dict):
            # value 如 expression: "steps['flip-coin'].outputs.result == 'heads' ? steps.heads.outputs.result : steps.tails.outputs.result"
            if 'expression' in value:
                value['expression'] = self.fix_expression(value['expression'])
            return {
                "name": key,
                "valueFrom": value
            }
        return {
            "name": key,
            "value": value
        }

    def build_steps(self, steps: Union[str, list]):
        '''
        构建steps, 参考 https://argoproj.github.io/argo-workflows/walk-through/steps/
        :param steps:
        :return:
        '''
        # 多个步骤
        if isinstance(steps, list):
            return {
                "steps": list(map(self.build_step, steps))
            }

        # 多个步骤，带步骤名
        if isinstance(steps, dict):
            return {
                "steps": [self.build_step(step, name) for name, step in steps.items()]
            }

        # 单个步骤
        return [self.build_step(steps)]

    #@replace_var_on_params
    def build_step(self, step: Union[str, list, dict], name=None, type='steps'):
        '''
        构建step
           兼容steps/dag的tasks中单个步骤的构建，兼容以下情况： 1 step参数类型不同 2 步骤自动命名不同 3 输出变量不同
           steps延迟替换变量, 因为下一步的输入变量会依赖上一步的输出
        :param step: 步骤信息, steps中会是list/str/dict, dag的tasks是str
        :param name: 指定步骤名
        :param type: 类型： 1 steps 2 tasks(dag)
        :return: 
        '''
        # list要递归，因为steps中--代表顺序执行，- 代表并行执行，也就是可能有多层list，要递归调用
        if isinstance(step, list):
            return list(map(self.build_step, step))

        # steps延迟替换变量, 因为下一步的输入变量会依赖上一步的输出
        step = replace_var(step)
        # 构建dict: template+when
        if isinstance(step, str):
            step = {
                'template': step
            }
        template = get_and_del_dict_item(step, 'template')
        # 解析步骤名
        if isinstance(template, str) and '=' in template:  # 遇到有=，则 步骤名=模板调用
            # name, template = template.split('=') # 检查分割, 不能处理参数带=的情况
            reg = r'^([^\(]+)='
            mat = re.search(reg, template) # 正则分割
            if mat is not None:
                name = mat.group(1)
                template = re.sub(reg, '', template)
        if name is None:
            name = get_and_del_dict_item(step, 'name')
            # 根据模板表达式自动命名步骤，必须根据模板+参数，不能根据解析后的模板(只有函数名, 不带参数, 无法确定唯一名)
            if name is None:
                if type == 'steps':
                    name = self.namer.build_name(template)  # steps生成步骤名, 不缓存(遇同名模板计数递增)
                else:
                    name = self.namer.get_name(template)  # tasks生成步骤名, 带缓存
        # 解析模板(函数调用)
        flow_ref, template, args = self.parse_step_call(template)
        # 拼接步骤
        step["name"] = name
        if flow_ref is None: # 本流程中的模板
            step['template'] = template
        else: # 引用其他 WorkflowTemplate 的模板
            step['templateRef'] = {
                'name': flow_ref,
                'template': template,
            }
        if args:
            step['arguments'] = self.build_step_call_args(template, args, flow_ref)
        # 输出变量
        self.build_step_output_vars(template, name, type)
        return step

    def parse_step_call(self, template):
        '''
        解析模板(函数)调用
           支持解析出对其他 WorkflowTemplate 的引用
           TODO: 要拿到其他 WorkflowTemplate 的模板的入参，否则无法拼接调用参数
        :param template:
        :return:
        '''
        # 解析出对其他 WorkflowTemplate 的引用
        flow_ref = None
        reg = r'^([^\.\(]+)\.'
        mat = re.search(reg, template)  # 正则分割
        if mat is not None:
            flow_ref = mat.group(1)
            template = re.sub(reg, '', template)
        # 解析模板调用(函数调用形式)
        template, args = parse_func(template, True)
        return flow_ref, template, args

    # 构建steps调用的参数
    def build_step_call_args(self, tpl_name, vals, flow_ref):
        # 输入参数名
        if flow_ref is None: # 当前流程
            names = self._template_inputs[tpl_name]
        else: # 其他流程
            names = self._flow2template_inputs[flow_ref][tpl_name]
        # if len(names) != len(vals):
        #     raise Exception(f"调用模板{tpl_name}的参数个数与声明的参数个数不一致")

        args = dict(zip(names, vals))
        return self.build_dict_args(args, 'call')

    def build_step_output_vars(self, tpl_name, step_name, type):
        '''
        构建steps调用的输出变量
        :param tpl_name:
        :param step_name: 步骤名作为变量名，变量值是输出参数的dict
        :param type: 类型： 1 steps 2 tasks(dag)，用做输出变量的前缀，如
                     steps: {{steps.generate.outputs.artifacts.out-artifact}}
                     tasks: {{tasks.generate-artifact.outputs.artifacts.hello-art}}
        :return:
        '''
        # 输出参数名
        names = self._template_outputs.get(tpl_name)
        # 遍历输出的参数，来构建变量值
        vals = {}
        if names:
            for name in names:
                if name.startswith('@'):  # artifacts: {{steps.generate.outputs.artifacts.out-artifact}}
                    val = '{{' + type + '.' + step_name + '.outputs.artifacts.' + name[1:] + '}}'
                else:  # parameters: {{steps.generate.outputs.parameters.out-parameter}}
                    val = '{{' + type + '.' + step_name + '.outputs.parameters.' + name + '}}'
                vals[name] = val
        # 设置result变量值，注：不建议输出参数名用result
        if 'result' not in vals:
            vals['result'] = '{{' + type + '.' + step_name + '.outputs.result}}'
        # 设置变量: 步骤名作为变量名，变量值是输出参数的dict
        set_var(step_name, vals)

    def build_suspend(self, option):
        if not option:
            return {
                "suspend": {}
            }

        if isinstance(option, (int, str)):
            duration = option
        else:
            duration = option['duration']
        return {
            "suspend": {
                "duration": str(duration) # Must be a string. Default unit is seconds. Could also be a Duration, e.g.: "2m", "6h", "1d"
            }
        }

    # 流程资源文件
    def build_apply(self, option):
        return self.build_res_action("apply", option)

    # 删除文件
    def build_delete(self, option):
        return self.build_res_action("delete", option)

    # 操作资源
    def build_res_action(self, action, option):
        # 源码
        src = get_and_del_dict_item(option, "manifest")
        if 'file' in option:
            src = read_file(get_and_del_dict_item(option, "file"))
        return {
            "resource": {
                "action": action,
                "manifest": src
            }
        }

    def build_dag(self, option):
        '''
        构建dag(依赖关系), 参考 https://argoproj.github.io/argo-workflows/walk-through/dag/
        :param option {deps, tasks} 二选一
                    deps: 多行依赖关系表达式，每行格式如下 echo(A) -> echo(B);echo(C) -> echo(D)
                    tasks: 类似steps中每一步的数据结构，只是可能会多出 dependencies 属性
        :return:
        '''
        if isinstance(option, str):
            option = {'deps': [option]}
        elif isinstance(option, dict):
            option = {'tasks': option}
        elif isinstance(option, list):
            if isinstance(option[0], dict): # 元素是dict的为tasks
                option = {'tasks': option}
            else: # 元素为str的为依赖关系表达式
                option = {'deps': option}

        # 1 根据依赖关系表达式，来构建任务
        if 'deps' in option:
            return self.build_dag_deps(option)

        # 2 直接构建任务
        return self.build_dag_tasks(option)

    # 直接构建任务, 类似 build_steps 的实现, dependencies属性要自行输入
    def build_dag_tasks(self, tasks):
        # 多个步骤，带步骤名
        if isinstance(tasks, dict):
            return {
                "dag": {
                    "tasks": [self.build_step(task, name, type='tasks') for name, task in tasks.items()]
                }
            }
        return {
            "dag": {
                "tasks": list(map(self.build_step, tasks, type='tasks'))
            }
        }

    # 根据依赖关系表达式，来构建任务
    def build_dag_deps(self, option: dict):
        deps = get_and_del_dict_item(option, 'deps')
        tasks = []
        for dep in deps:
            # 去掉空格
            # dep = dep.replace(' ', '')
            dep = re.sub(r'\s*(->|;|,)\s*', lambda m: m.group(1), dep)
            if not dep:
                continue
            items = dep.split('->')  # 分割每个点
            items = list(map(lambda x: x.split(';'), items))  # 点中有点, 分号分割
            # 首个点: 无依赖
            for node in items[0]:
                task = self.build_dag_task_dep(node)
                tasks.append(task)
            # 后续的点: 依赖于前一个点
            for i in range(1, len(items)):
                item = items[i]
                for node in item:
                    task = self.build_dag_task_dep(node, items[i - 1])  # 前一个点为依赖节点
                    tasks.append(task)
        return {
            "dag": {
                "tasks": tasks,
                **option
            }
        }

    def build_dag_task_dep(self, node, dep_nodes=None):
        '''
        构建dag的单个任务依赖
        :param node: 当前节点
        :param dep_nodes: 依赖的节点
        :return:
        '''
        task = self.build_step(node, type='tasks')
        if dep_nodes:
            task["dependencies"] = list(map(self.namer.get_name, dep_nodes))
        return task

# cli入口
def main():
    # 读元数据：author/version/description
    dir = os.path.dirname(__file__)
    meta = read_init_file_meta(dir + os.sep + '__init__.py')
    # 步骤配置的yaml
    step_files, option = parse_cmd('ArgoFlowBoot', meta['version'])
    if len(step_files) == 0:
        raise Exception("Miss step config file or directory")
    # 基于yaml的执行器
    boot = Boot(option.output)
    try:
        # 执行yaml配置的步骤
        boot.run(step_files)
    except Exception as ex:
        log.error(f"Exception occurs: current step file is %s", boot.step_file, exc_info=ex)
        raise ex

if __name__ == '__main__':
    main()
    # data = read_yaml('/home/shi/code/python/ArgoFlowBoot/example/test.yml')
    # print(json.dumps(data))
