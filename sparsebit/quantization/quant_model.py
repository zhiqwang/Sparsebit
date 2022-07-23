from atexit import register
import copy
import operator
import importlib
from functools import partial
from fnmatch import fnmatch
from yacs.config import CfgNode as CN
from collections import defaultdict

import torch
import torch.nn as nn
import torch.fx as fx
import torch.nn.functional as F
import onnx

from sparsebit.utils import update_config
from sparsebit.quantization.modules import *
from sparsebit.quantization.observers import Observer
from sparsebit.quantization.quantizers import Quantizer
from sparsebit.quantization.tools import QuantizationErrorProfiler
from sparsebit.quantization.converters import simplify, fuse_operations

__all__ = ["QuantModel"]


class QuantModel(nn.Module):
    def __init__(self, model: nn.Module, config):
        super().__init__()
        self.model = model
        self.cfg = config
        self.device = torch.device(config.DEVICE)
        self._run_simplifiers()
        self._convert2quantmodule()
        self._build_quantizer()
        self._run_fuse_operations()

    def _convert2quantmodule(self):
        """
        将网络中所有node转成对应的quant_module
        """
        named_modules = dict(self.model.named_modules(remove_duplicate=False))
        traced = fx.symbolic_trace(self.model)
        traced.graph.print_tabular()
        modules_viewed = {}
        qnodes = []  # 用于避免重复遍历
        for n in traced.graph.nodes:
            if not isinstance(n, fx.Node) or n in qnodes:
                continue
            elif n.op == "call_module":
                assert n.target in named_modules, "no found {} in model".format(
                    n.target
                )
                org_module = named_modules[n.target]
                new_module = QMODULE_MAP[type(org_module)](org_module)
            elif n.op == "call_function":
                new_module = QMODULE_MAP[n.target](n)  # node作为module传入获取相关参数
            elif n.op == "call_method":
                new_module = QMODULE_MAP[n.target](n)  # node作为module传入获取相关参数
            elif n.op in ["placeholder", "get_attr", "output"]:
                continue
            with traced.graph.inserting_after(n):
                traced.add_module(n.name, new_module)
                new_node = traced.graph.call_module(n.name, n.args, n.kwargs)
                qnodes.append(new_node)
                n.replace_all_uses_with(
                    new_node
                )  # n的输出全部接到new_node, n成为no user节点(即可删除)
                traced.graph.erase_node(n)
        traced.recompile()
        self.model = fx.GraphModule(traced, traced.graph)

    def _build_quantizer(self):
        """
        递归对每个QuantModule建立quantizer
        """

        def _probe(module_name: str, specific_modules: dict):
            for k, v in specific_modules.items():
                if fnmatch(module_name, k):
                    return True, v
            return False, None

        def _sub_build(src, module_name):
            sub_cfg = src.clone()
            is_match, specific_config = (
                _probe(module_name, sub_cfg.SPECIFIC[0])
                if src.SPECIFIC
                else (False, None)
            )
            if is_match:
                sub_cfg.merge_from_list(specific_config)
            update_config(sub_cfg, "SPECIFIC", [])
            return sub_cfg

        # build config for every QuantModule
        for n, m in self.model.named_modules():
            if isinstance(m, QuantOpr):
                _config = self.cfg.clone()  # init
                update_config(_config, "W", _sub_build(self.cfg.W, n))
                update_config(_config, "A", _sub_build(self.cfg.A, n))
                m.build_quantizer(_config)

    def _run_simplifiers(self):
        self.model = simplify(self.model)

    def _run_fuse_operations(self):
        self.model = fuse_operations(self.model, self.cfg.SCHEDULE)
        self.model.graph.print_tabular()

    def prepare_calibration(self):
        """
        对与input相接的QuantOpr注册hook, (weight_quantizer不需要)
        """
        # TODO: 如果有两个输入怎么
        from sparsebit.quantization.tools.calibration import get_topo, register_hook

        self.eval()
        self.calib_cache = get_topo(self.model)
        self._calibration_handles = register_hook(self.model, self.calib_cache)

    def calc_qparams(self):
        from sparsebit.quantization.tools.calibration import (
            feature_layerwise_calibration,
        )

        for h in self._calibration_handles:
            h.remove()

        # Feature calibration
        feature_layerwise_calibration(self.model, self.calib_cache, self.device)

        # Weight calibration
        for n, m in self.model.named_modules():
            if isinstance(m, QuantOpr) and m.weight_quantizer:
                m.weight_quantizer.update_observer(m.weight)
                m.weight_quantizer.calc_qparams()

    def init_QAT(self):
        named_modules = dict(self.model.named_modules())
        # disable quant of input, note: not full-test
        input_nodes = [n for n in self.model.graph.nodes if n.op == "placeholder"]
        for input_node in input_nodes:
            input_users = list(input_node.users)
            while len(input_users) > 0:
                _user = input_users.pop()  # 弹出最后一个元素
                _module = named_modules[_user.target]
                if isinstance(_module, PASSTHROUGHT_MODULES):
                    input_users.extend(list(_user.users))
                else:
                    _module.input_quantizer.set_fake_fused()  # 有bug, quant_state会来回切.
        self.calc_qparams()
        self.set_quant(w_quant=True, a_quant=True)
        self.enable_qat = True  # flag, 留备用

    def set_lastmodule_wbit(self, bit=8):
        named_modules = dict(self.model.named_modules())
        output_nodes = [n for n in self.model.graph.nodes if n.op == "output"]
        for out_node in output_nodes:
            inputs_outn = [a for a in out_node.args if isinstance(a, torch.fx.Node)]
            while len(inputs_outn) > 0:
                n = inputs_outn.pop()
                m = named_modules[n.target]
                if hasattr(m, "weight_quantizer") and m.weight_quantizer:
                    m.weight_quantizer.set_bit(bit)
                else:
                    n_list = [a for a in n.args if isinstance(a, torch.fx.Node)]
                    inputs_outn.extend(n_list)

    def forward(self, *args):
        return self.model.forward(*args)

    def get_quantization_error(
        self, data: torch.Tensor, checker=F.mse_loss, is_async: bool = True
    ):

        from sparsebit.quantization.tools import QuantizationErrorProfiler

        return QuantizationErrorProfiler(self.model).apply(data, checker, is_async)

    def set_quant(self, w_quant=False, a_quant=False):
        for n, m in self.model.named_modules():
            if isinstance(m, QuantOpr):
                m.set_quant(w_quant, a_quant)

    def export_onnx(
        self,
        dummy_data,
        name,
        input_names=None,
        output_names=None,
        dynamic_axes=None,
        opset_version=13,
        verbose=False,
        extra_info=False,
    ):
        self.eval()
        self.set_quant(w_quant=True, a_quant=True)  # quant must prepared before export
        for n, m in self.model.named_modules():
            if isinstance(m, Quantizer):
                m.enable_export_onnx()
                if m.bit != 8:
                    assert (
                        extra_info
                    ), "You must set extra_info=True when export a model with {}bit".format(
                        m.bit
                    )

        torch.onnx.export(
            self.model.cpu(),
            dummy_data.cpu(),
            name,
            opset_version=opset_version,
            input_names=input_names,
            output_names=output_names,
            dynamic_axes=dynamic_axes,
            verbose=verbose,
        )
        for n, m in self.model.named_modules():
            if isinstance(m, Quantizer):
                m.disable_export_onnx()

        if extra_info:
            self.add_extra_info_to_onnx(name)

    def add_extra_info_to_onnx(self, onnx_path):
        onnx_model = onnx.load(onnx_path)
        extra_onnx_path = onnx_path.replace(".onnx", "_extra.onnx")
        tensor_inputs = {}
        tensor_outputs = {}
        nodes = {}
        for op in onnx_model.graph.node:
            nodes[op.name] = op
            for inp in op.input:
                if inp not in tensor_outputs:
                    tensor_outputs[inp] = []
                tensor_outputs[inp].append(op.name)
            for outp in op.output:
                if outp not in tensor_inputs:
                    tensor_inputs[outp] = []
                tensor_inputs[outp].append(op.name)

        op_pos = 0
        skipped_modules = set()
        for name, module in self.model.named_modules():
            if (
                module == self.model
                or isinstance(module, (Observer, Quantizer))
                or module in skipped_modules
            ):
                continue
            if isinstance(module, QuantOpr):
                for submodule in module.children():
                    if not isinstance(submodule, QuantOpr):
                        skipped_modules.add(submodule)

            while op_pos < len(onnx_model.graph.node) and (
                onnx_model.graph.node[op_pos].op_type
                in ["QuantizeLinear", "DequantizeLinear", "Constant"]
            ):
                op_pos += 1
            onnx_op = onnx_model.graph.node[op_pos]
            op_pos += 1

            if isinstance(module, QuantOpr) and getattr(
                module.input_quantizer, "is_enable", False
            ):
                input_dequant = nodes[tensor_inputs[onnx_op.input[0]][0]]
                input_quant = nodes[tensor_inputs[input_dequant.input[0]][0]]
                input_dequant.attribute.append(
                    onnx.helper.make_attribute("bits", module.input_quantizer.bit)
                )
                input_quant.attribute.append(
                    onnx.helper.make_attribute("bits", module.input_quantizer.bit)
                )

            if isinstance(module, QuantOpr) and getattr(
                module.weight_quantizer, "is_enable", False
            ):
                weight_dequant = nodes[tensor_inputs[onnx_op.input[1]][0]]
                weight_quant = nodes[tensor_inputs[weight_dequant.input[0]][0]]
                weight_dequant.attribute.append(
                    onnx.helper.make_attribute("bits", module.weight_quantizer.bit)
                )
                weight_quant.attribute.append(
                    onnx.helper.make_attribute("bits", module.weight_quantizer.bit)
                )
        onnx.save(onnx_model, extra_onnx_path)